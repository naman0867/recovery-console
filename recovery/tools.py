"""
Tools available to the recovery agent.

The important one is `check_issuer_health`. In production you do not get told
"HDFC UPI is down" - you infer it from your own recent traffic, late and
noisily. So this tool computes health from a trailing window of observed
failures and never reads the simulator's downtime ground truth.

That gap between inferred health and actual downtime is where the agent's
decisions get interesting, and it is why the evaluation reports a detection
lag rather than assuming perfect knowledge.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta

WINDOW = timedelta(minutes=20)

# Outage detection is baseline-relative, not an absolute count.
#
# A fixed threshold ("6 failures in 20 minutes = outage") only calibrates for
# one traffic volume. HDFC UPI and CNRB emandate differ by two orders of
# magnitude, so the same number is a quiet afternoon for one and a total outage
# for the other. Instead we compare the current window against that specific
# (issuer, method) pair's own long-run failure rate and flag a burst.
OUTAGE_RATIO = 3.0          # current window vs expected, to call an outage
DEGRADED_RATIO = 1.8
MIN_EVENTS_OUTAGE = 4       # floor, so 1-vs-0.2 does not read as a 5x burst
MIN_EVENTS_DEGRADED = 3


@dataclass
class IssuerHealth:
    issuer: str
    method: str
    status: str              # healthy | degraded | outage
    failures_in_window: int
    expected_in_window: float
    burst_ratio: float
    suggested_hold_minutes: int
    confidence: float


class IssuerHealthMonitor:
    """Per-(issuer, method) burst detector.

    Maintains a rolling 20-minute count and a long-run rate for each pair, then
    reports how far above its own normal the pair currently is. This is
    inference from observed traffic only - it never reads the simulator's
    downtime ground truth, so it detects outages late and imperfectly, exactly
    as a production system would.
    """

    def __init__(self):
        self._events: dict[tuple[str, str], deque[datetime]] = defaultdict(deque)
        self._total: dict[tuple[str, str], int] = defaultdict(int)
        self._first: dict[tuple[str, str], datetime] = {}
        self._last: dict[tuple[str, str], datetime] = {}

    def observe(self, issuer: str, method: str, at: datetime) -> None:
        key = (issuer, method)
        q = self._events[key]
        q.append(at)
        while q and q[0] < at - WINDOW:
            q.popleft()
        self._total[key] += 1
        self._first.setdefault(key, at)
        self._last[key] = at

    def _expected(self, key: tuple[str, str], at: datetime) -> float:
        """Expected failures in one window, from this pair's long-run rate."""
        first = self._first.get(key)
        if first is None:
            return 0.0
        elapsed = (at - first).total_seconds()
        if elapsed < WINDOW.total_seconds():
            return 0.0
        rate_per_sec = self._total[key] / elapsed
        return rate_per_sec * WINDOW.total_seconds()

    def check(self, issuer: str, method: str, at: datetime) -> IssuerHealth:
        key = (issuer, method)
        q = self._events[key]
        while q and q[0] < at - WINDOW:
            q.popleft()
        n = len(q)
        expected = self._expected(key, at)

        if expected <= 0:
            # Not enough history to judge. Say healthy rather than guess -
            # a false outage call costs real money through staleness decay.
            return IssuerHealth(issuer, method, "healthy", n, 0.0, 0.0, 0, 0.50)

        ratio = n / expected

        if ratio >= OUTAGE_RATIO and n >= MIN_EVENTS_OUTAGE:
            status = "outage"
            hold = 45
            conf = min(0.95, 0.55 + 0.10 * (ratio - OUTAGE_RATIO))
        elif ratio >= DEGRADED_RATIO and n >= MIN_EVENTS_DEGRADED:
            status = "degraded"
            hold = 25
            conf = min(0.80, 0.45 + 0.12 * (ratio - DEGRADED_RATIO))
        else:
            status, hold, conf = "healthy", 0, 0.80

        return IssuerHealth(
            issuer, method, status, n,
            round(expected, 2), round(ratio, 2), hold, round(conf, 2),
        )


def get_customer_history(payment: dict) -> dict:
    """Reliability signal from the customer's own payment record."""
    ok = payment.get("customer_prior_success", 0)
    bad = payment.get("customer_prior_failure", 0)
    total = ok + bad
    rate = ok / total if total else 0.5
    return {
        "prior_success": ok,
        "prior_failure": bad,
        "success_rate": round(rate, 3),
        "segment": "reliable" if rate >= 0.8 and total >= 5
        else "unproven" if total < 5
        else "flaky",
        "reachable": ok > 0,
    }


ALTERNATE_RAIL = {
    "card": "upi",
    "netbanking": "upi",
    "upi": "card",
    "upi_autopay": "upi",
    "emandate": "card",
}


def schedule_retry(
    payment: dict,
    at: datetime,
    delay_minutes: int,
    rail: str | None = None,
    attempt: int = 1,
) -> dict:
    """Build a retry instruction. Does not execute it - the runner does."""
    method = rail or payment["method"]
    return {
        "action": "retry",
        "payment_id": payment["payment_id"],
        "rail": method,
        "rail_switched": method != payment["method"],
        "scheduled_at": (at + timedelta(minutes=delay_minutes)).isoformat(timespec="seconds"),
        "delay_minutes": delay_minutes,
        "attempt": attempt,
    }


NUDGE_TEMPLATES = {
    "BAD_REQUEST_ERROR:insufficient_funds":
        "Your payment of {amount} didn't go through - the bank reported low balance. "
        "Top up and retry here, the order is held for 24 hours.",
    "BAD_REQUEST_ERROR:card_expired":
        "Your saved card has expired, so the payment of {amount} couldn't be taken. "
        "Add a new card to keep the order.",
    "BAD_REQUEST_ERROR:invalid_vpa":
        "The UPI ID on file isn't valid any more. Pay {amount} with a different UPI ID or card.",
    "BAD_REQUEST_ERROR:mandate_revoked":
        "Your autopay mandate was cancelled, so we couldn't collect {amount}. "
        "Set up autopay again to avoid a break in service.",
}

DEFAULT_NUDGE = (
    "We couldn't collect {amount} for your order. Nothing has been charged. "
    "Complete the payment here when you're ready."
)


def draft_nudge(payment: dict) -> dict:
    """Deterministic template selection, keyed on the error code.

    Free-text generation is not used here on purpose: a model that invents an
    amount or a refund promise in a customer-facing payment message is a
    compliance problem, not a feature. Templates are reviewed once; generated
    text would need reviewing every time.
    """
    amount = f"Rs {payment['amount_paise'] / 100:,.2f}"
    template = NUDGE_TEMPLATES.get(payment.get("error_code") or "", DEFAULT_NUDGE)
    return {
        "action": "nudge",
        "payment_id": payment["payment_id"],
        "channel": "whatsapp" if payment.get("customer_prior_success", 0) > 0 else "email",
        "body": template.format(amount=amount),
        "template_id": payment.get("error_code") or "generic_v1",
    }
