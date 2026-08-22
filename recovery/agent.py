"""
The recovery agent.

Given one failed payment it produces exactly one Decision: an archetype, a
concrete action, a confidence, and a sentence of reasoning that has to hold up
in the audit log.

The loop is: triage the error -> gather context from tools -> apply policy ->
emit action. Policy is deterministic given the tool outputs, which is a
deliberate choice: the model's judgement is confined to classifying unfamiliar
error text, and everything downstream of that is inspectable code. A reviewer
can read this file and predict what the system will do.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .tools import (
    IssuerHealthMonitor,
    get_customer_history,
    schedule_retry,
    draft_nudge,
    ALTERNATE_RAIL,
)
from .triage import Triager


@dataclass
class Decision:
    payment_id: str
    archetype: str
    action: dict[str, Any] | None
    confidence: float
    reasoning: str
    tier: str
    tool_calls: list[str] = field(default_factory=list)
    model_cost_usd: float = 0.0
    latency_ms: float = 0.0


class RecoveryAgent:
    """Ablation flags exist so evaluate.py can attribute the lift to a specific
    capability rather than reporting one opaque headline number."""

    def __init__(
        self,
        triager: Triager | None = None,
        monitor: IssuerHealthMonitor | None = None,
        use_health: bool = True,
        use_nudge: bool = True,
        use_rail_switch: bool = True,
        suppress_unrecoverable: bool = True,
    ):
        self.triager = triager or Triager()
        self.monitor = monitor or IssuerHealthMonitor()
        self.use_health = use_health
        self.use_nudge = use_nudge
        self.use_rail_switch = use_rail_switch
        self.suppress_unrecoverable = suppress_unrecoverable

    def decide(self, payment: dict, now: datetime) -> Decision:
        calls: list[str] = []

        triage = self.triager.classify(payment)
        calls.append(f"triage[{triage.tier}]")

        health = self.monitor.check(payment["issuer"], payment["method"], now)
        calls.append("check_issuer_health")

        history = get_customer_history(payment)
        calls.append("get_customer_history")

        archetype = triage.archetype
        confidence = triage.confidence
        notes: list[str] = [triage.reason]

        # Health overrides classification. An error code that normally means
        # "retry now" means "wait" when the issuer is visibly on fire, and a
        # rail switch is pointless if the customer's alternate rail is fine.
        if not self.use_rail_switch and archetype == "SWITCH_RAIL":
            archetype = "RETRY_NOW"
        if not self.use_nudge and archetype == "CUSTOMER_ACTION":
            archetype = "RETRY_NOW"
        if not self.suppress_unrecoverable and archetype == "UNRECOVERABLE":
            archetype = "RETRY_NOW"

        if self.use_health and health.status == "outage" and archetype in (
            "RETRY_NOW", "RETRY_AFTER_WINDOW", "SWITCH_RAIL"
        ):
            # The outage is scoped to this issuer AND this method. If the
            # customer has another rail available, routing around the outage
            # beats waiting for it to clear. Mandates cannot switch rails
            # mid-cycle, so those still wait.
            if self.use_rail_switch and not payment.get("is_recurring"):
                archetype = "SWITCH_RAIL"
                notes.append(
                    f"{health.issuer}/{health.method} bursting "
                    f"{health.burst_ratio}x baseline, routing around"
                )
            else:
                archetype = "RETRY_AFTER_WINDOW"
                notes.append(
                    f"{health.issuer}/{health.method} bursting "
                    f"{health.burst_ratio}x baseline, mandate must wait"
                )
            confidence = min(0.92, (confidence + health.confidence) / 2 + 0.15)
        elif self.use_health and health.status == "degraded" and archetype == "RETRY_NOW":
            archetype = "RETRY_AFTER_WINDOW"
            confidence = max(confidence * 0.9, 0.4)
            notes.append(f"{health.issuer} degraded, holding")

        # A customer who has paid reliably before is worth one more attempt;
        # a flaky one is not worth burning a rail switch on.
        if history["segment"] == "reliable" and archetype != "UNRECOVERABLE":
            confidence = min(0.95, confidence + 0.05)
        elif history["segment"] == "flaky" and archetype == "SWITCH_RAIL":
            confidence *= 0.85
            notes.append("flaky payer, low expected value on rail switch")

        action = self._build_action(payment, archetype, health, history, now)
        if action:
            calls.append(action["action"] if action["action"] == "nudge" else "schedule_retry")

        self.monitor.observe(payment["issuer"], payment["method"], now)

        return Decision(
            payment_id=payment["payment_id"],
            archetype=archetype,
            action=action,
            confidence=round(min(confidence, 0.99), 3),
            reasoning="; ".join(notes)[:180],
            tier=triage.tier,
            tool_calls=calls,
            model_cost_usd=triage.model_cost_usd,
            latency_ms=triage.latency_ms,
        )

    def _build_action(self, payment, archetype, health, history, now) -> dict | None:
        if archetype == "UNRECOVERABLE":
            return None

        if archetype == "RETRY_NOW":
            return schedule_retry(payment, now, delay_minutes=4)

        if archetype == "RETRY_AFTER_WINDOW":
            hold = health.suggested_hold_minutes or 30
            return schedule_retry(payment, now, delay_minutes=hold)

        if archetype == "SWITCH_RAIL":
            return schedule_retry(
                payment, now, delay_minutes=8,
                rail=ALTERNATE_RAIL.get(payment["method"], "upi"),
            )

        if archetype == "CUSTOMER_ACTION":
            return draft_nudge(payment)

        return None
