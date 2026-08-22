"""
Synthetic failed-payment generator.

Design notes, because these assumptions are the first thing a reviewer should
attack:

  * Failures are NOT independent. Real payment failure is bursty: an issuer's
    authorisation stack degrades and every payment routed to it fails for the
    same reason for the next 20-90 minutes. We model that explicitly with
    downtime windows per (issuer, method). Without this, every retry policy
    scores identically and the whole problem becomes uninteresting.

  * Traffic follows an Indian e-commerce diurnal curve, with a lunchtime bump
    and a large 20:00-23:00 peak. Downtime is more likely during peak, which is
    also when it is most expensive.

  * Amounts are lognormal per method. UPI skews small, cards and netbanking
    skew larger, mandates cluster at subscription price points.

  * ~6% of failures carry no catalogued error code, only a free-text string
    from the acquirer. This is the long tail the LLM classifier handles.

Everything here is a modelling assumption, not measured data. The values that
matter most for the headline result are `DOWNTIME_SHARE` and the per-code
`base_retry_success` in error_codes.py; those are the two to validate first
against real traffic.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta

from .error_codes import CATALOG, LONG_TAIL, Truth

ISSUERS = [
    ("HDFC", 0.185), ("SBIN", 0.170), ("ICIC", 0.140), ("UTIB", 0.105),
    ("KKBK", 0.075), ("PUNB", 0.070), ("BARB", 0.060), ("IDIB", 0.050),
    ("YESB", 0.045), ("INDB", 0.040), ("FDRL", 0.035), ("CNRB", 0.025),
]

METHODS = [
    ("upi", 0.520), ("card", 0.245), ("netbanking", 0.135),
    ("upi_autopay", 0.070), ("emandate", 0.030),
]

# (mu, sigma) of the underlying lognormal over PAISE.
# Medians: UPI ~Rs 450, card ~Rs 1,800, netbanking ~Rs 3,200,
# UPI autopay ~Rs 600, emandate ~Rs 1,500 (subscription price points).
AMOUNT_PARAMS = {
    "upi": (10.71, 1.05),
    "card": (12.10, 1.15),
    "netbanking": (12.68, 1.00),
    "upi_autopay": (11.00, 0.55),
    "emandate": (11.92, 0.60),
}

MIN_AMOUNT_PAISE = 10_00            # Rs 10
MAX_AMOUNT_PAISE = 2_00_000_00      # Rs 2,00,000

# Share of all generated failures that land inside an issuer downtime window.
# This is the single most load-bearing assumption in the simulation.
DOWNTIME_SHARE = 0.27

HOUR_WEIGHTS = [
    0.012, 0.007, 0.005, 0.004, 0.005, 0.009,   # 00-05
    0.018, 0.031, 0.044, 0.052, 0.056, 0.058,   # 06-11
    0.061, 0.057, 0.048, 0.045, 0.047, 0.055,   # 12-17
    0.068, 0.082, 0.095, 0.088, 0.062, 0.031,   # 18-23
]


@dataclass
class DowntimeWindow:
    issuer: str
    method: str
    start: datetime
    end: datetime
    severity: float          # 0.55-0.98, how total the outage is

    def covers(self, issuer: str, method: str, at: datetime) -> bool:
        return (
            self.issuer == issuer
            and self.method == method
            and self.start <= at <= self.end
        )


@dataclass
class FailedPayment:
    payment_id: str
    order_id: str
    customer_id: str
    amount_paise: int
    currency: str
    method: str
    issuer: str
    failed_at: str
    error_code: str | None
    error_source: str
    error_step: str
    error_description: str
    is_long_tail: bool
    customer_prior_success: int
    customer_prior_failure: int
    is_recurring: bool
    # ---- hidden from the agent, consumed by simulator.py only ----
    _truth: Truth
    _in_downtime: bool
    _downtime_ends: str | None

    def observable(self) -> dict:
        """The redacted record handed to triage and the agent."""
        d = asdict(self)
        for hidden in ("_truth", "_in_downtime", "_downtime_ends"):
            d.pop(hidden, None)
        return d


def _weighted(rng: random.Random, pairs: list[tuple[str, float]]) -> str:
    return rng.choices([p[0] for p in pairs], weights=[p[1] for p in pairs])[0]


def _sample_hour(rng: random.Random) -> int:
    return rng.choices(range(24), weights=HOUR_WEIGHTS)[0]


def _sample_amount(rng: random.Random, method: str) -> int:
    mu, sigma = AMOUNT_PARAMS[method]
    paise = int(rng.lognormvariate(mu, sigma))
    return max(MIN_AMOUNT_PAISE, min(paise, MAX_AMOUNT_PAISE))


def _build_downtime(rng: random.Random, day0: datetime, days: int) -> list[DowntimeWindow]:
    """Roughly 3-6 outages per day, biased toward evening peak."""
    windows: list[DowntimeWindow] = []
    for day in range(days):
        for _ in range(rng.randint(3, 6)):
            issuer = _weighted(rng, ISSUERS)
            method = _weighted(rng, METHODS)
            hour = _sample_hour(rng)
            start = day0 + timedelta(days=day, hours=hour, minutes=rng.randint(0, 59))
            windows.append(
                DowntimeWindow(
                    issuer=issuer,
                    method=method,
                    start=start,
                    end=start + timedelta(minutes=rng.randint(18, 95)),
                    severity=rng.uniform(0.55, 0.98),
                )
            )
    return windows


def generate(
    n: int = 50_000,
    days: int = 14,
    seed: int = 7,
    start: datetime | None = None,
) -> tuple[list[FailedPayment], list[DowntimeWindow]]:
    rng = random.Random(seed)
    day0 = (start or datetime(2026, 8, 1, 0, 0, 0)).replace(minute=0, second=0, microsecond=0)
    windows = _build_downtime(rng, day0, days)

    downtime_codes = [e for e in CATALOG if e.truth.downtime_linked]
    normal_codes = [e for e in CATALOG if not e.truth.downtime_linked]

    customers: dict[str, list[int]] = {}
    payments: list[FailedPayment] = []

    for i in range(n):
        want_downtime = rng.random() < DOWNTIME_SHARE and windows

        if want_downtime:
            w = rng.choice(windows)
            issuer, method = w.issuer, w.method
            span = (w.end - w.start).total_seconds()
            ts = w.start + timedelta(seconds=rng.uniform(0, span))
            in_downtime, ends = True, w.end
        else:
            issuer = _weighted(rng, ISSUERS)
            method = _weighted(rng, METHODS)
            day = rng.randrange(days)
            ts = day0 + timedelta(
                days=day, hours=_sample_hour(rng),
                minutes=rng.randint(0, 59), seconds=rng.randint(0, 59),
            )
            in_downtime = any(w.covers(issuer, method, ts) for w in windows)
            ends = next((w.end for w in windows if w.covers(issuer, method, ts)), None)

        is_long_tail = rng.random() < 0.06

        if is_long_tail:
            text, truth = rng.choice(LONG_TAIL)
            code, source, step, desc = None, "gateway", "payment_authorization", text
        else:
            pool = downtime_codes if in_downtime and rng.random() < 0.72 else normal_codes
            pool = [e for e in pool if method in e.methods] or [
                e for e in CATALOG if method in e.methods
            ]
            err = rng.choices(pool, weights=[e.weight for e in pool])[0]
            truth = err.truth
            code, source, step, desc = err.code, err.source, err.step, err.description

        cust = f"cust_{rng.randrange(1, max(2, n // 4)):06d}"
        hist = customers.setdefault(cust, [rng.randint(0, 24), rng.randint(0, 5)])

        payments.append(
            FailedPayment(
                payment_id=f"pay_{i:07d}{rng.randrange(16**5):05x}",
                order_id=f"order_{i:07d}",
                customer_id=cust,
                amount_paise=_sample_amount(rng, method),
                currency="INR",
                method=method,
                issuer=issuer,
                failed_at=ts.isoformat(timespec="seconds"),
                error_code=code,
                error_source=source,
                error_step=step,
                error_description=desc,
                is_long_tail=is_long_tail,
                customer_prior_success=hist[0],
                customer_prior_failure=hist[1],
                is_recurring=method in ("upi_autopay", "emandate"),
                _truth=truth,
                _in_downtime=in_downtime,
                _downtime_ends=ends.isoformat(timespec="seconds") if ends else None,
            )
        )

    payments.sort(key=lambda p: p.failed_at)
    return payments, windows
