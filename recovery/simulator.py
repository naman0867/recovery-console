"""
Outcome simulator - the ground truth arbiter.

Given a payment and a proposed action, decides whether the money actually comes
back. This reads the hidden `_truth` block that the agent never sees, which is
what makes the evaluation meaningful rather than circular: the agent is scored
on outcomes it could not observe when deciding.

Effects modelled, in order of how much they move the result:

  * Downtime timing. Retrying inside an outage window is heavily penalised;
    retrying after it clears gets a large bonus. This is the effect the whole
    RETRY_AFTER_WINDOW archetype exists to exploit.
  * Attempt decay. Each successive attempt on the same payment is worth less.
  * Amount sensitivity. Balance and limit failures get worse as amounts rise.
  * Customer reliability. Prior success rate shifts both retry and nudge odds.
  * Channel reach. A nudge to an unreachable customer mostly does nothing.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass
class Outcome:
    recovered: bool
    amount_paise: int
    attempted: bool
    wasted: bool             # an attempt was spent and nothing came back
    channel: str


class OutcomeSimulator:
    def __init__(self, seed: int = 11):
        self.rng = random.Random(seed)

    def resolve(self, payment, action: dict | None, attempt: int = 1) -> Outcome:
        """`payment` is a FailedPayment (with hidden truth), not the dict view."""
        if not action:
            return Outcome(False, 0, False, False, "none")

        truth = payment._truth
        amount = payment.amount_paise
        failed_at = datetime.fromisoformat(payment.failed_at)

        if action["action"] == "nudge":
            p = truth.customer_action_success
            reach = payment.customer_prior_success
            p *= 1.0 if reach > 3 else 0.72 if reach > 0 else 0.35
            if amount > 10_000_00:
                p *= 0.80
            success = self.rng.random() < max(0.0, min(p, 0.95))
            return Outcome(success, amount if success else 0, True, not success, "nudge")

        # --- retry paths ---
        scheduled = datetime.fromisoformat(action["scheduled_at"])
        switched = action.get("rail_switched", False)
        p = truth.switch_rail_success if switched else truth.base_retry_success

        # Downtime is scoped to one (issuer, method) pair. Re-presenting on a
        # different rail routes around the outage entirely, so the penalty and
        # the post-clearance bonus both apply to same-rail retries only.
        if truth.downtime_linked and payment._downtime_ends and not switched:
            ends = datetime.fromisoformat(payment._downtime_ends)
            if scheduled <= ends:
                # Still inside the outage. Collapse the odds.
                p *= 0.18
            else:
                # Cleared, and not so late that the customer moved on.
                minutes_after = (scheduled - ends).total_seconds() / 60
                p = min(0.93, p * (2.6 if minutes_after <= 60 else 1.9))

        # Attempt decay.
        p *= (0.72 ** (attempt - 1))

        # Amount sensitivity for balance/limit style failures.
        if truth.amount_sensitive:
            p *= 1.12 if amount < 1_000_00 else 0.88 if amount < 10_000_00 else 0.61

        # Customer reliability.
        total = payment.customer_prior_success + payment.customer_prior_failure
        if total >= 5:
            rate = payment.customer_prior_success / total
            p *= 0.80 + 0.45 * rate

        # Staleness: a retry two hours after the customer abandoned checkout
        # loses intent, unless it was a scheduled mandate.
        delay_h = (scheduled - failed_at).total_seconds() / 3600
        if not payment.is_recurring and delay_h > 1.5:
            p *= max(0.55, 1.0 - 0.10 * (delay_h - 1.5))

        p = max(0.0, min(p, 0.95))
        success = self.rng.random() < p
        channel = "retry_switched" if switched else "retry_same_rail"
        return Outcome(success, amount if success else 0, True, not success, channel)
