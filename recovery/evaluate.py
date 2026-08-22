"""
Evaluation harness.

Replays one identical, seeded stream of failed payments through both policies
and reports the difference. Same payments, same order, same outcome RNG seed
per arm, so the only variable is the decision.

Reported per arm:
  recovery rate        recovered payments / total failures
  recovered value      rupees actually returned
  wasted attempts      actions spent that recovered nothing
  attempt efficiency   recovered / attempted
  cost per decision    model spend / decisions
  p50 / p95 latency    decision latency

Plus a triage breakdown separating rules-tier from model-tier accuracy, which
is the number that justifies (or kills) the LLM in the pipeline.
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from dataclasses import dataclass, asdict
from datetime import datetime
from pathlib import Path

from .agent import RecoveryAgent
from .baseline import BaselinePolicy
from .generator import generate
from .guardrails import Guardrails
from .simulator import OutcomeSimulator
from .tools import IssuerHealthMonitor
from .triage import Triager


@dataclass
class ArmResult:
    name: str
    total_payments: int
    total_value_paise: int
    recovered_count: int
    recovered_paise: int
    attempts: int
    wasted_attempts: int
    held_for_approval: int
    blocked: int
    decisions: int
    model_cost_usd: float
    p50_latency_ms: float
    p95_latency_ms: float
    by_archetype: dict

    @property
    def recovery_rate(self) -> float:
        return self.recovered_count / self.total_payments if self.total_payments else 0.0

    @property
    def value_recovery_rate(self) -> float:
        return self.recovered_paise / self.total_value_paise if self.total_value_paise else 0.0

    @property
    def attempt_efficiency(self) -> float:
        return self.recovered_count / self.attempts if self.attempts else 0.0

    @property
    def cost_per_decision_usd(self) -> float:
        return self.model_cost_usd / self.decisions if self.decisions else 0.0


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    idx = min(len(s) - 1, int(round(q * (len(s) - 1))))
    return s[idx]


def run_arm(name, policy, payments, seed: int = 11, guardrails: Guardrails | None = None):
    sim = OutcomeSimulator(seed=seed)
    guards = guardrails or Guardrails()
    latencies: list[float] = []
    by_archetype: dict[str, dict] = defaultdict(
        lambda: {"decided": 0, "recovered": 0, "recovered_paise": 0, "wasted": 0}
    )

    recovered_count = recovered_paise = attempts = wasted = 0
    model_cost = 0.0

    for p in payments:
        now = datetime.fromisoformat(p.failed_at)
        view = p.observable()

        t0 = time.perf_counter()
        decision = policy.decide(view, now)
        wall = (time.perf_counter() - t0) * 1000
        latencies.append(max(wall, decision.latency_ms))
        model_cost += decision.model_cost_usd

        verdict = guards.evaluate(view, decision, now)
        guards.commit(view, decision, verdict, now)

        bucket = by_archetype[decision.archetype]
        bucket["decided"] += 1

        if not verdict.allowed:
            continue

        outcome = sim.resolve(p, decision.action, attempt=1)
        if outcome.attempted:
            attempts += 1
        if outcome.recovered:
            recovered_count += 1
            recovered_paise += outcome.amount_paise
            bucket["recovered"] += 1
            bucket["recovered_paise"] += outcome.amount_paise
        elif outcome.wasted:
            wasted += 1
            bucket["wasted"] += 1

    stats = guards.stats()
    return ArmResult(
        name=name,
        total_payments=len(payments),
        total_value_paise=sum(p.amount_paise for p in payments),
        recovered_count=recovered_count,
        recovered_paise=recovered_paise,
        attempts=attempts,
        wasted_attempts=wasted,
        held_for_approval=stats["held_for_approval"],
        blocked=stats["blocked"],
        decisions=stats["decisions"],
        model_cost_usd=model_cost,
        p50_latency_ms=_percentile(latencies, 0.50),
        p95_latency_ms=_percentile(latencies, 0.95),
        by_archetype=dict(by_archetype),
    ), guards


def _rupees(paise: int) -> str:
    return f"Rs {paise / 100:,.0f}"


def format_table(base: ArmResult, agent: ArmResult) -> str:
    def row(label, a, b, delta=""):
        return f"  {label:<26} {a:>18} {b:>18} {delta:>14}"

    lift = agent.recovered_paise - base.recovered_paise
    pct = (lift / base.recovered_paise * 100) if base.recovered_paise else 0.0
    waste_delta = agent.wasted_attempts - base.wasted_attempts

    lines = [
        "",
        "=" * 80,
        f"  RECOVERY EVALUATION - {base.total_payments:,} failed payments, "
        f"{_rupees(base.total_value_paise)} at risk",
        "=" * 80,
        row("", "BASELINE", "AGENT", "DELTA"),
        "  " + "-" * 76,
        row("recovery rate", f"{base.recovery_rate:.2%}", f"{agent.recovery_rate:.2%}",
            f"{(agent.recovery_rate - base.recovery_rate) * 100:+.2f}pp"),
        row("value recovered", _rupees(base.recovered_paise), _rupees(agent.recovered_paise),
            f"{pct:+.1f}%"),
        row("payments recovered", f"{base.recovered_count:,}", f"{agent.recovered_count:,}",
            f"{agent.recovered_count - base.recovered_count:+,}"),
        row("attempts spent", f"{base.attempts:,}", f"{agent.attempts:,}",
            f"{agent.attempts - base.attempts:+,}"),
        row("wasted attempts", f"{base.wasted_attempts:,}", f"{agent.wasted_attempts:,}",
            f"{waste_delta:+,}"),
        row("attempt efficiency", f"{base.attempt_efficiency:.2%}",
            f"{agent.attempt_efficiency:.2%}",
            f"{(agent.attempt_efficiency - base.attempt_efficiency) * 100:+.2f}pp"),
        "  " + "-" * 76,
        row("held for approval", f"{base.held_for_approval:,}", f"{agent.held_for_approval:,}"),
        row("blocked by guardrails", f"{base.blocked:,}", f"{agent.blocked:,}"),
        row("cost per decision", f"${base.cost_per_decision_usd:.6f}",
            f"${agent.cost_per_decision_usd:.6f}"),
        row("p50 latency", f"{base.p50_latency_ms:.3f} ms", f"{agent.p50_latency_ms:.3f} ms"),
        row("p95 latency", f"{base.p95_latency_ms:.3f} ms", f"{agent.p95_latency_ms:.3f} ms"),
        "=" * 80,
        "",
        "  AGENT DECISIONS BY ARCHETYPE",
        "  " + "-" * 76,
        f"  {'archetype':<24}{'decided':>10}{'recovered':>12}{'hit rate':>12}{'value':>18}",
    ]

    for name, b in sorted(agent.by_archetype.items(), key=lambda kv: -kv[1]["decided"]):
        hit = b["recovered"] / b["decided"] if b["decided"] else 0
        lines.append(
            f"  {name:<24}{b['decided']:>10,}{b['recovered']:>12,}"
            f"{hit:>11.1%}{_rupees(b['recovered_paise']):>18}"
        )

    lines += ["  " + "-" * 76, ""]
    return "\n".join(lines)


ABLATIONS = [
    ("full agent", {}),
    ("- issuer health", {"use_health": False}),
    ("- rail switching", {"use_rail_switch": False}),
    ("- customer nudges", {"use_nudge": False}),
    ("- unrecoverable suppression", {"suppress_unrecoverable": False}),
]


def run_ablations(payments, triager) -> str:
    """Turn one capability off at a time and measure what it was worth.

    A capability that costs nothing to remove does not belong in the pitch.
    """
    rows = []
    full_value = None

    for label, flags in ABLATIONS:
        policy = RecoveryAgent(triager=triager, monitor=IssuerHealthMonitor(), **flags)
        result, _ = run_arm(label, policy, payments)
        if full_value is None:
            full_value = result.recovered_paise
        delta = result.recovered_paise - full_value
        rows.append((label, result, delta))

    lines = [
        "  ABLATION - value recovered with each capability removed",
        "  " + "-" * 76,
        f"  {'variant':<32}{'recovered':>16}{'rate':>10}{'wasted':>10}{'vs full':>14}",
    ]
    for label, r, delta in rows:
        vs = "-" if delta == 0 else f"{_rupees(delta)}"
        lines.append(
            f"  {label:<32}{_rupees(r.recovered_paise):>16}"
            f"{r.recovery_rate:>9.1%}{r.wasted_attempts:>10,}{vs:>14}"
        )
    lines += ["  " + "-" * 76, ""]
    return "\n".join(lines)


def main(n: int = 50_000, days: int = 14, seed: int = 7, out: str = "data",
         ablate: bool = True):
    print(f"generating {n:,} failed payments across {days} days ...")
    payments, windows = generate(n=n, days=days, seed=seed)
    print(f"  {len(windows)} issuer downtime windows")
    print(f"  {sum(1 for p in payments if p._in_downtime):,} payments inside an outage")
    print(f"  {sum(1 for p in payments if p.is_long_tail):,} uncatalogued (long-tail) errors")

    triager = Triager()
    print(f"  triage tier 2: {'model' if triager.use_model else 'keyword fallback (no API key)'}")

    print("\nrunning baseline ...")
    base, _ = run_arm("baseline", BaselinePolicy(), payments)

    print("running agent ...")
    agent_policy = RecoveryAgent(triager=triager, monitor=IssuerHealthMonitor())
    agent, guards = run_arm("agent", agent_policy, payments)

    table = format_table(base, agent)
    print(table)

    if ablate:
        print("running ablations ...")
        table += "\n" + run_ablations(payments, triager)
        print(table.split("ABLATION")[-1].join(["  ABLATION", ""]))

    outdir = Path(out)
    outdir.mkdir(parents=True, exist_ok=True)
    guards.export(outdir / "audit_log.jsonl")
    (outdir / "metrics.json").write_text(json.dumps({
        "baseline": {**asdict(base), "recovery_rate": base.recovery_rate,
                     "attempt_efficiency": base.attempt_efficiency},
        "agent": {**asdict(agent), "recovery_rate": agent.recovery_rate,
                  "attempt_efficiency": agent.attempt_efficiency},
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "config": {"n": n, "days": days, "seed": seed,
                   "model_tier_active": triager.use_model},
    }, indent=2, default=str))
    (outdir / "metrics.txt").write_text(table)

    print(f"  wrote {outdir/'metrics.json'}, {outdir/'metrics.txt'}, {outdir/'audit_log.jsonl'}")
    return base, agent


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=50_000)
    ap.add_argument("--days", type=int, default=14)
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--out", type=str, default="data")
    ap.add_argument("--no-ablate", action="store_true")
    args = ap.parse_args()
    main(n=args.n, days=args.days, seed=args.seed, out=args.out, ablate=not args.no_ablate)
