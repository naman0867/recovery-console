"""
Guardrails.

Everything the agent decides passes through here before it can become an
action. The question this file answers is: if the model is confidently wrong
across 10,000 payments at 02:00, what is the blast radius?

Five controls, in the order they fire:

  1. Kill switch      - one flag halts all outbound actions, decisions still
                        get logged so you can see what it would have done.
  2. Idempotency      - SHA-256 of (payment_id, attempt, rail). A replayed or
                        double-consumed decision collapses to one action.
  3. Attempt cap      - hard ceiling per payment, independent of what the
                        agent asks for. Also a per-customer daily cap so one
                        customer cannot be nudged fourteen times.
  4. Approval queue   - actions above a rupee threshold, or below a confidence
                        floor, are held for a human instead of executed.
  5. Audit log        - every decision, its inputs, its tier, its reasoning and
                        its guardrail verdict, appended immutably.

Anything blocked is recorded, never silently dropped.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

MAX_ATTEMPTS_PER_PAYMENT = 3
MAX_ACTIONS_PER_CUSTOMER_PER_DAY = 4
APPROVAL_AMOUNT_PAISE = 25_000_00        # Rs 25,000
MIN_CONFIDENCE_TO_EXECUTE = 0.35


@dataclass
class Verdict:
    allowed: bool
    status: str              # executed | held_for_approval | blocked | suppressed
    reason: str
    idempotency_key: str


@dataclass
class AuditEntry:
    ts: str
    payment_id: str
    amount_paise: int
    archetype: str
    tier: str
    confidence: float
    reasoning: str
    action: dict[str, Any]
    status: str
    guardrail_reason: str
    idempotency_key: str


class Guardrails:
    def __init__(
        self,
        max_attempts: int = MAX_ATTEMPTS_PER_PAYMENT,
        approval_threshold_paise: int = APPROVAL_AMOUNT_PAISE,
        min_confidence: float = MIN_CONFIDENCE_TO_EXECUTE,
        kill_switch: bool = False,
        dry_run: bool = False,
    ):
        self.max_attempts = max_attempts
        self.approval_threshold = approval_threshold_paise
        self.min_confidence = min_confidence
        self.kill_scopes: set[str] = {"all"} if kill_switch else set()
        self.dry_run = dry_run

        self._attempts: dict[str, int] = defaultdict(int)
        self._customer_day: dict[tuple[str, str], int] = defaultdict(int)
        self._seen_keys: set[str] = set()
        self.audit: list[AuditEntry] = []
        self.approval_queue: list[AuditEntry] = []

    @staticmethod
    def idempotency_key(payment_id: str, attempt: int, rail: str) -> str:
        return hashlib.sha256(f"{payment_id}|{attempt}|{rail}".encode()).hexdigest()[:24]

    def evaluate(
        self,
        payment: dict,
        decision: "Decision",  # noqa: F821 - avoids a circular import
        now: datetime,
    ) -> Verdict:
        action = decision.action or {}
        rail = action.get("rail", payment["method"])
        attempt = self._attempts[payment["payment_id"]] + 1
        key = self.idempotency_key(payment["payment_id"], attempt, rail)

        matched = self._matching_scope(payment, decision)
        if matched:
            return Verdict(False, "suppressed", f"kill switch active: {matched}", key)

        if self.dry_run:
            return Verdict(False, "dry_run", "dry run - decided but not executed", key)

        if key in self._seen_keys:
            return Verdict(False, "blocked", "duplicate idempotency key", key)

        if decision.archetype == "UNRECOVERABLE":
            return Verdict(False, "blocked", "classified unrecoverable, no spend", key)

        if not action:
            return Verdict(False, "blocked", "no action proposed", key)

        if self._attempts[payment["payment_id"]] >= self.max_attempts:
            return Verdict(False, "blocked", f"attempt cap {self.max_attempts} reached", key)

        day = now.date().isoformat()
        if self._customer_day[(payment["customer_id"], day)] >= MAX_ACTIONS_PER_CUSTOMER_PER_DAY:
            return Verdict(False, "blocked", "per-customer daily action cap reached", key)

        if decision.confidence < self.min_confidence:
            return Verdict(False, "held_for_approval", "confidence below execution floor", key)

        if payment["amount_paise"] >= self.approval_threshold:
            return Verdict(
                False, "held_for_approval",
                f"amount above Rs {self.approval_threshold / 100:,.0f} threshold", key,
            )

        return Verdict(True, "executed", "within policy", key)

    def commit(self, payment: dict, decision, verdict: Verdict, now: datetime) -> AuditEntry:
        if verdict.allowed:
            self._seen_keys.add(verdict.idempotency_key)
            self._attempts[payment["payment_id"]] += 1
            self._customer_day[(payment["customer_id"], now.date().isoformat())] += 1

        entry = AuditEntry(
            ts=now.isoformat(timespec="seconds"),
            payment_id=payment["payment_id"],
            amount_paise=payment["amount_paise"],
            archetype=decision.archetype,
            tier=decision.tier,
            confidence=round(decision.confidence, 3),
            reasoning=decision.reasoning,
            action=decision.action or {},
            status=verdict.status,
            guardrail_reason=verdict.reason,
            idempotency_key=verdict.idempotency_key,
        )
        self.audit.append(entry)
        if verdict.status == "held_for_approval":
            self.approval_queue.append(entry)
        return entry

    def _matching_scope(self, payment: dict, decision) -> str | None:
        """Return the first kill-switch scope that suppresses this action."""
        if not self.kill_scopes:
            return None
        if "all" in self.kill_scopes:
            return "all"

        action = (decision.action or {}).get("action")
        candidates = [
            f"issuer:{payment['issuer']}",
            f"method:{payment['method']}",
            "nudges" if action == "nudge" else "retries" if action == "retry" else None,
        ]
        for c in candidates:
            if c and c in self.kill_scopes:
                return c
        return None

    # ---- operator surface -------------------------------------------------

    @property
    def kill_switch(self) -> bool:
        """Back-compat: is anything suppressed at all."""
        return bool(self.kill_scopes)

    def engage_kill_switch(self, scope: str = "all") -> None:
        self.kill_scopes.add(scope)

    def release_kill_switch(self, scope: str | None = None) -> None:
        if scope is None:
            self.kill_scopes.clear()
        else:
            self.kill_scopes.discard(scope)

    def set_dry_run(self, enabled: bool) -> None:
        self.dry_run = enabled

    def stats(self) -> dict:
        counts: dict[str, int] = defaultdict(int)
        for e in self.audit:
            counts[e.status] += 1
        return {
            "decisions": len(self.audit),
            "executed": counts["executed"],
            "held_for_approval": counts["held_for_approval"],
            "blocked": counts["blocked"],
            "suppressed": counts["suppressed"],
            "held_value_paise": sum(e.amount_paise for e in self.approval_queue),
            "kill_switch": self.kill_switch,
            "kill_scopes": sorted(self.kill_scopes),
            "dry_run": self.dry_run,
            "dry_run_count": counts["dry_run"],
        }

    def export(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as fh:
            for e in self.audit:
                fh.write(json.dumps(asdict(e)) + "\n")

