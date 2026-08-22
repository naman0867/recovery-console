"""
Replay session backing the console.

Holds a generated stream of failed payments and advances a cursor through it on
demand. Each advance runs one payment through the full pipeline - triage,
agent, guardrails, outcome - and appends the result to a bounded feed the UI
polls. Deterministic, single-threaded, and restartable, so a demo behaves the
same way twice.
"""

from __future__ import annotations

import threading
from collections import deque
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any

from recovery.agent import RecoveryAgent, Decision
from recovery.baseline import BaselinePolicy
from recovery.generator import generate
from recovery.guardrails import Guardrails
from recovery.simulator import OutcomeSimulator
from recovery.tools import IssuerHealthMonitor
from recovery.triage import Triager

FEED_LIMIT = 400
AUDIT_LIMIT = 2000


@dataclass
class FeedRow:
    payment_id: str
    order_id: str
    customer_id: str
    failed_at: str
    amount_paise: int
    method: str
    issuer: str
    error_code: str | None
    error_description: str
    is_long_tail: bool
    archetype: str
    action: dict[str, Any] | None
    confidence: float
    reasoning: str
    tier: str
    tool_calls: list[str]
    status: str
    guardrail_reason: str
    idempotency_key: str
    recovered: bool
    recovered_paise: int
    issuer_status: str
    burst_ratio: float


class RecoverySession:
    def __init__(self, n: int = 50_000, days: int = 14, seed: int = 7):
        self._lock = threading.Lock()
        self.config = {"n": n, "days": days, "seed": seed}
        self.reset()

    # -- lifecycle ---------------------------------------------------------

    def reset(self) -> None:
        with self._lock:
            self.payments, self.windows = generate(**self.config)
            self.triager = Triager()
            self.monitor = IssuerHealthMonitor()
            self.agent = RecoveryAgent(triager=self.triager, monitor=self.monitor)
            self.baseline = BaselinePolicy()
            self.guards = Guardrails()
            self.sim = OutcomeSimulator(seed=11)
            self.baseline_sim = OutcomeSimulator(seed=11)
            self.baseline_guards = Guardrails()

            self.cursor = 0
            self.feed: deque[FeedRow] = deque(maxlen=FEED_LIMIT)
            self.totals = {
                "processed": 0,
                "at_risk_paise": 0,
                "recovered_paise": 0,
                "recovered_count": 0,
                "baseline_recovered_paise": 0,
                "baseline_recovered_count": 0,
                "wasted": 0,
                "attempts": 0,
                "model_cost_usd": 0.0,
                "by_archetype": {},
            }

    # -- stepping ----------------------------------------------------------

    def advance(self, count: int = 12) -> list[FeedRow]:
        with self._lock:
            rows: list[FeedRow] = []
            for _ in range(count):
                if self.cursor >= len(self.payments):
                    break
                rows.append(self._step(self.payments[self.cursor]))
                self.cursor += 1
            return rows

    def _step(self, payment) -> FeedRow:
        now = datetime.fromisoformat(payment.failed_at)
        view = payment.observable()

        health = self.monitor.check(payment.issuer, payment.method, now)
        decision: Decision = self.agent.decide(view, now)
        verdict = self.guards.evaluate(view, decision, now)
        self.guards.commit(view, decision, verdict, now)

        outcome = (
            self.sim.resolve(payment, decision.action, attempt=1)
            if verdict.allowed
            else None
        )

        b_decision = self.baseline.decide(view, now)
        b_verdict = self.baseline_guards.evaluate(view, b_decision, now)
        self.baseline_guards.commit(view, b_decision, b_verdict, now)
        b_outcome = (
            self.baseline_sim.resolve(payment, b_decision.action, attempt=1)
            if b_verdict.allowed
            else None
        )

        t = self.totals
        t["processed"] += 1
        t["at_risk_paise"] += payment.amount_paise
        t["model_cost_usd"] += decision.model_cost_usd

        bucket = t["by_archetype"].setdefault(
            decision.archetype, {"decided": 0, "recovered": 0, "recovered_paise": 0}
        )
        bucket["decided"] += 1

        if outcome:
            if outcome.attempted:
                t["attempts"] += 1
            if outcome.recovered:
                t["recovered_paise"] += outcome.amount_paise
                t["recovered_count"] += 1
                bucket["recovered"] += 1
                bucket["recovered_paise"] += outcome.amount_paise
            elif outcome.wasted:
                t["wasted"] += 1

        if b_outcome and b_outcome.recovered:
            t["baseline_recovered_paise"] += b_outcome.amount_paise
            t["baseline_recovered_count"] += 1

        row = FeedRow(
            payment_id=payment.payment_id,
            order_id=payment.order_id,
            customer_id=payment.customer_id,
            failed_at=payment.failed_at,
            amount_paise=payment.amount_paise,
            method=payment.method,
            issuer=payment.issuer,
            error_code=payment.error_code,
            error_description=payment.error_description,
            is_long_tail=payment.is_long_tail,
            archetype=decision.archetype,
            action=decision.action,
            confidence=decision.confidence,
            reasoning=decision.reasoning,
            tier=decision.tier,
            tool_calls=decision.tool_calls,
            status=verdict.status,
            guardrail_reason=verdict.reason,
            idempotency_key=verdict.idempotency_key,
            recovered=bool(outcome and outcome.recovered),
            recovered_paise=outcome.amount_paise if outcome and outcome.recovered else 0,
            issuer_status=health.status,
            burst_ratio=health.burst_ratio,
        )
        self.feed.appendleft(row)
        return row

    # -- read surfaces -----------------------------------------------------

    def snapshot(self) -> dict:
        t = self.totals
        base = t["baseline_recovered_paise"]
        lift = ((t["recovered_paise"] - base) / base * 100) if base else 0.0
        processed = t["processed"] or 1
        return {
            "processed": t["processed"],
            "total": len(self.payments),
            "at_risk_paise": t["at_risk_paise"],
            "recovered_paise": t["recovered_paise"],
            "recovered_count": t["recovered_count"],
            "recovery_rate": t["recovered_count"] / processed,
            "baseline_recovered_paise": base,
            "baseline_recovery_rate": t["baseline_recovered_count"] / processed,
            "lift_pct": lift,
            "wasted": t["wasted"],
            "attempts": t["attempts"],
            "attempt_efficiency": t["recovered_count"] / (t["attempts"] or 1),
            "model_cost_usd": t["model_cost_usd"],
            "by_archetype": t["by_archetype"],
            "guardrails": self.guards.stats(),
            "model_tier": "model" if self.triager.use_model else "fallback",
        }

    def feed_rows(self, limit: int = 60) -> list[dict]:
        return [asdict(r) for r in list(self.feed)[:limit]]

    def issuer_board(self) -> list[dict]:
        """Current health for every (issuer, method) pair with live traffic."""
        if not self.feed:
            return []
        now = datetime.fromisoformat(self.feed[0].failed_at)
        seen, board = set(), []
        for row in list(self.feed)[:200]:
            key = (row.issuer, row.method)
            if key in seen:
                continue
            seen.add(key)
            h = self.monitor.check(row.issuer, row.method, now)
            board.append({
                "issuer": h.issuer, "method": h.method, "status": h.status,
                "failures_in_window": h.failures_in_window,
                "expected_in_window": h.expected_in_window,
                "burst_ratio": h.burst_ratio, "confidence": h.confidence,
            })
        order = {"outage": 0, "degraded": 1, "healthy": 2}
        board.sort(key=lambda b: (order[b["status"]], -b["burst_ratio"]))
        return board[:14]

    def approvals(self, limit: int = 40) -> list[dict]:
        return [asdict(e) for e in self.guards.approval_queue[-limit:][::-1]]

    def audit(self, limit: int = 120, query: str = "") -> list[dict]:
        rows = [asdict(e) for e in self.guards.audit[-AUDIT_LIMIT:][::-1]]
        if query:
            q = query.lower()
            rows = [
                r for r in rows
                if q in r["payment_id"].lower()
                or q in r["archetype"].lower()
                or q in r["status"].lower()
                or q in r["reasoning"].lower()
                or q in r["guardrail_reason"].lower()
            ]
        return rows[:limit]

    def resolve_approval(self, idempotency_key: str, approve: bool) -> dict | None:
        with self._lock:
            for i, entry in enumerate(self.guards.approval_queue):
                if entry.idempotency_key != idempotency_key:
                    continue
                self.guards.approval_queue.pop(i)
                entry.status = "executed" if approve else "blocked"
                entry.guardrail_reason = (
                    "approved by operator" if approve else "rejected by operator"
                )
                if approve:
                    self.guards._seen_keys.add(entry.idempotency_key)
                    self.guards._attempts[entry.payment_id] += 1
                return asdict(entry)
            return None

    # Bands are chosen to match the delays the agent actually schedules:
    # RETRY_NOW is +4 min, SWITCH_RAIL +8, a degraded hold is +25, an outage
    # hold +45. Wider bands (6hr, 24hr) would always render empty, so they are
    # deliberately absent rather than shown as dead rows.
    AGE_BANDS = [
        ("0-5 min", 0, 5),
        ("5-15 min", 5, 15),
        ("15-30 min", 15, 30),
        ("30-60 min", 30, 60),
        ("60 min+", 60, 10 ** 9),
    ]

    def age_curve(self) -> list[dict]:
        """Recovery probability as a function of how stale the failure was.

        Bucketed by the delay between failure and the action the agent
        scheduled. The decay is real - it comes out of the staleness term in
        the outcome simulator, not from a hand-drawn curve - and it is the
        clearest argument for acting on failures quickly.
        """
        buckets = {label: {"n": 0, "recovered": 0, "value": 0, "recovered_value": 0}
                   for label, _, _ in self.AGE_BANDS}

        for row in self.feed:
            action = row.action or {}
            if not action.get("scheduled_at"):
                continue
            delay = (
                datetime.fromisoformat(action["scheduled_at"])
                - datetime.fromisoformat(row.failed_at)
            ).total_seconds() / 60
            for label, lo, hi in self.AGE_BANDS:
                if lo <= delay < hi:
                    b = buckets[label]
                    b["n"] += 1
                    b["value"] += row.amount_paise
                    if row.recovered:
                        b["recovered"] += 1
                        b["recovered_value"] += row.recovered_paise
                    break

        return [
            {
                "band": label,
                "attempted": b["n"],
                "recovered": b["recovered"],
                "probability": (b["recovered"] / b["n"]) if b["n"] else 0.0,
                "value_paise": b["value"],
                "recovered_value_paise": b["recovered_value"],
            }
            for label, _, _ in self.AGE_BANDS
            for b in [buckets[label]]
        ]

    def issuer_detail(self, issuer: str, method: str) -> dict:
        """Drill-down for one (issuer, method) pair.

        Sample size is returned alongside every rate on purpose: a 3x burst
        built from one transaction is not evidence, and the UI needs enough
        information to say so rather than implying an incident.
        """
        rows = [r for r in self.feed if r.issuer == issuer and r.method == method]
        if not rows:
            return {"issuer": issuer, "method": method, "found": False}

        now = datetime.fromisoformat(self.feed[0].failed_at)
        health = self.monitor.check(issuer, method, now)

        codes: dict[str, int] = {}
        for r in rows:
            key = r.error_code or "uncatalogued"
            codes[key] = codes.get(key, 0) + 1
        top = sorted(codes.items(), key=lambda kv: -kv[1])[:4]

        at_risk = sum(r.amount_paise for r in rows)
        recovered = sum(r.recovered_paise for r in rows)
        degraded_since = min(
            (r.failed_at for r in rows if r.issuer_status != "healthy"), default=None
        )

        return {
            "found": True,
            "issuer": issuer,
            "method": method,
            "status": health.status,
            "failures_in_window": health.failures_in_window,
            "expected_in_window": health.expected_in_window,
            "burst_ratio": health.burst_ratio,
            "sample_sufficient": health.failures_in_window >= 4,
            "observed_payments": len(rows),
            "at_risk_paise": at_risk,
            "recovered_paise": recovered,
            "recovery_rate": (recovered / at_risk) if at_risk else 0.0,
            "top_error_codes": [{"code": c, "count": n} for c, n in top],
            "degraded_since": degraded_since,
            "suggested_action": (
                "Route new traffic off this rail; hold same-rail retries."
                if health.status == "outage"
                else "Watch. Not enough evidence to act."
                if not health.failures_in_window >= 4
                else "Reduce retry rate on this pair."
                if health.status == "degraded"
                else "No action needed."
            ),
        }

    def action_required(self) -> dict:
        """Counts for the above-the-fold strip."""
        board = self.issuer_board()
        alerts = [b for b in board if b["status"] in ("outage", "degraded")]
        follow_ups = sum(
            1 for r in self.feed
            if r.archetype == "CUSTOMER_ACTION" and r.status == "executed"
        )
        return {
            "approvals": len(self.guards.approval_queue),
            "held_value_paise": sum(e.amount_paise for e in self.guards.approval_queue),
            "issuer_alerts": len(alerts),
            "alerting_pairs": [f"{a['issuer']}/{a['method']}" for a in alerts[:4]],
            "follow_ups": follow_ups,
        }

    def set_kill_switch(self, engaged: bool, scope: str = "all") -> dict:
        with self._lock:
            if engaged:
                self.guards.engage_kill_switch(scope)
            else:
                self.guards.release_kill_switch(None if scope == "all" else scope)
            return self.guards.stats()

    def set_dry_run(self, enabled: bool) -> dict:
        with self._lock:
            self.guards.set_dry_run(enabled)
            return self.guards.stats()
