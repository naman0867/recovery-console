"""
Baseline policy: retry everything once, same rail, +30 minutes.

This is what most merchants actually run, and it is the control arm. It is
deliberately not a strawman - a flat +30m retry does recover real money, and
any honest evaluation has to show how much of the agent's headline number is
just "retrying at all" versus "retrying intelligently".
"""

from __future__ import annotations

from datetime import datetime

from .agent import Decision
from .tools import schedule_retry

FIXED_DELAY_MINUTES = 30


class BaselinePolicy:
    name = "fixed +30m same-rail retry"

    def decide(self, payment: dict, now: datetime) -> Decision:
        return Decision(
            payment_id=payment["payment_id"],
            archetype="RETRY_NOW",
            action=schedule_retry(payment, now, delay_minutes=FIXED_DELAY_MINUTES),
            confidence=1.0,
            reasoning="baseline: unconditional single retry",
            tier="rules",
            tool_calls=["schedule_retry"],
        )
