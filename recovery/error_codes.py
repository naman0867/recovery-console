"""
Razorpay-style payment failure taxonomy.

Two things live in this file and they must not be confused:

  1. PUBLIC fields  - what the recovery agent is allowed to see at decision time
                      (code, source, step, description, method applicability).
  2. GROUND TRUTH   - `truth` block, used ONLY by simulator.py to decide whether
                      a retry actually succeeds. The agent never reads this.

Keeping the hidden parameters in the same file makes the taxonomy readable, but
`public_view()` is the only accessor the agent pipeline is permitted to call.
Codes and descriptions follow the shape of Razorpay's published error reference.
"""

from dataclasses import dataclass, field
from typing import Literal

Archetype = Literal[
    "RETRY_NOW",
    "RETRY_AFTER_WINDOW",
    "SWITCH_RAIL",
    "CUSTOMER_ACTION",
    "UNRECOVERABLE",
]

ARCHETYPES: list[Archetype] = [
    "RETRY_NOW",
    "RETRY_AFTER_WINDOW",
    "SWITCH_RAIL",
    "CUSTOMER_ACTION",
    "UNRECOVERABLE",
]

ARCHETYPE_HELP = {
    "RETRY_NOW": "Transient failure. Re-present the same payment on the same rail.",
    "RETRY_AFTER_WINDOW": "Issuer or rail is degraded. Hold, then re-present once it clears.",
    "SWITCH_RAIL": "This rail will keep declining. Re-present on a different method.",
    "CUSTOMER_ACTION": "Nothing works until the customer fixes something on their side.",
    "UNRECOVERABLE": "Do not spend another attempt on this payment.",
}


@dataclass(frozen=True)
class Truth:
    """Hidden generative parameters. Read only by the outcome simulator."""

    # Probability a same-rail retry succeeds under normal issuer conditions.
    base_retry_success: float
    # Probability a switch to a different method succeeds.
    switch_rail_success: float
    # Probability the customer resolves it after a nudge, within the window.
    customer_action_success: float
    # Does issuer downtime drive this failure? If so, retrying during an
    # outage is near-hopeless and retrying after it clears is near-certain.
    downtime_linked: bool = False
    # Does success get less likely as the amount climbs? (balance-type failures)
    amount_sensitive: bool = False


@dataclass(frozen=True)
class ErrorCode:
    code: str
    source: str          # bank | gateway | customer | business | issuer
    step: str            # payment_authentication | payment_authorization | payment_initiation
    description: str     # customer-facing text, as surfaced by the gateway
    methods: tuple[str, ...]
    weight: float        # relative frequency in generated traffic
    truth: Truth = field(repr=False)

    def public_view(self) -> dict:
        """Exactly what the triage layer and agent are allowed to observe."""
        return {
            "code": self.code,
            "source": self.source,
            "step": self.step,
            "description": self.description,
        }


# --------------------------------------------------------------------------
# The head of the distribution: codes the rules engine handles deterministically
# --------------------------------------------------------------------------

CATALOG: list[ErrorCode] = [
    ErrorCode(
        code="GATEWAY_ERROR",
        source="gateway",
        step="payment_authorization",
        description="Payment failed due to a temporary issue at the bank.",
        methods=("card", "netbanking", "upi"),
        weight=0.155,
        truth=Truth(0.24, 0.55, 0.10, downtime_linked=True),
    ),
    ErrorCode(
        code="BAD_REQUEST_ERROR:insufficient_funds",
        source="issuer",
        step="payment_authorization",
        description="Payment processing failed because of insufficient balance.",
        methods=("card", "netbanking", "upi"),
        weight=0.130,
        truth=Truth(0.17, 0.21, 0.46, amount_sensitive=True),
    ),
    ErrorCode(
        code="BAD_REQUEST_ERROR:payment_timeout",
        source="customer",
        step="payment_authentication",
        description="Payment was not completed on time.",
        methods=("upi", "netbanking"),
        weight=0.128,
        truth=Truth(0.41, 0.44, 0.33),
    ),
    ErrorCode(
        code="BAD_REQUEST_ERROR:invalid_otp",
        source="customer",
        step="payment_authentication",
        description="Payment failed because of an incorrect OTP.",
        methods=("card",),
        weight=0.092,
        truth=Truth(0.38, 0.47, 0.40),
    ),
    ErrorCode(
        code="GATEWAY_ERROR:issuer_unavailable",
        source="bank",
        step="payment_authorization",
        description="Payment could not be completed as the bank is not responding.",
        methods=("card", "netbanking", "upi"),
        weight=0.088,
        truth=Truth(0.12, 0.61, 0.08, downtime_linked=True),
    ),
    ErrorCode(
        code="BAD_REQUEST_ERROR:card_declined",
        source="issuer",
        step="payment_authorization",
        description="Card was declined by the issuing bank.",
        methods=("card",),
        weight=0.081,
        truth=Truth(0.14, 0.52, 0.19),
    ),
    ErrorCode(
        code="BAD_REQUEST_ERROR:upi_collect_declined",
        source="customer",
        step="payment_authentication",
        description="Payment request was declined by the customer in their UPI app.",
        methods=("upi",),
        weight=0.073,
        truth=Truth(0.29, 0.34, 0.37),
    ),
    ErrorCode(
        code="BAD_REQUEST_ERROR:limit_exceeded",
        source="issuer",
        step="payment_authorization",
        description="Payment failed as the transaction limit was exceeded.",
        methods=("card", "upi", "netbanking"),
        weight=0.055,
        truth=Truth(0.09, 0.58, 0.28, amount_sensitive=True),
    ),
    ErrorCode(
        code="BAD_REQUEST_ERROR:authentication_failed",
        source="issuer",
        step="payment_authentication",
        description="3DS authentication could not be completed for this card.",
        methods=("card",),
        weight=0.048,
        truth=Truth(0.22, 0.49, 0.31),
    ),
    ErrorCode(
        code="BAD_REQUEST_ERROR:invalid_vpa",
        source="customer",
        step="payment_initiation",
        description="The VPA entered is not valid.",
        methods=("upi",),
        weight=0.041,
        truth=Truth(0.03, 0.30, 0.52),
    ),
    ErrorCode(
        code="BAD_REQUEST_ERROR:card_expired",
        source="issuer",
        step="payment_authorization",
        description="Payment failed as the card has expired.",
        methods=("card",),
        weight=0.034,
        truth=Truth(0.01, 0.44, 0.49),
    ),
    ErrorCode(
        code="BAD_REQUEST_ERROR:mandate_revoked",
        source="customer",
        step="payment_authorization",
        description="Payment failed because the mandate has been revoked.",
        methods=("upi_autopay", "emandate"),
        weight=0.032,
        truth=Truth(0.02, 0.11, 0.41),
    ),
    ErrorCode(
        code="BAD_REQUEST_ERROR:mandate_not_active",
        source="bank",
        step="payment_authorization",
        description="The mandate is not yet active at the customer's bank.",
        methods=("upi_autopay", "emandate"),
        weight=0.024,
        truth=Truth(0.35, 0.18, 0.44, downtime_linked=True),
    ),
    ErrorCode(
        code="BAD_REQUEST_ERROR:payment_declined_risk",
        source="issuer",
        step="payment_authorization",
        description="Payment was declined by the issuer as a suspected risk.",
        methods=("card", "netbanking"),
        weight=0.019,
        truth=Truth(0.04, 0.07, 0.05),
    ),
]

BY_CODE: dict[str, ErrorCode] = {e.code: e for e in CATALOG}


# --------------------------------------------------------------------------
# The long tail: free-text gateway strings with no catalogued code.
#
# This is the ~6% of traffic that a pure rules engine cannot route. It exists
# so the LLM classifier in triage.py has a real job rather than a decorative
# one, and so the eval can measure what the model is actually worth.
# --------------------------------------------------------------------------

LONG_TAIL: list[tuple[str, Truth]] = [
    ("Transaction rejected: acquirer routing unavailable for this BIN range",
     Truth(0.19, 0.57, 0.09, downtime_linked=True)),
    ("Payer PSP did not respond within the stipulated interval",
     Truth(0.44, 0.40, 0.26, downtime_linked=True)),
    ("Debit not permitted on this account type for e-commerce merchants",
     Truth(0.02, 0.51, 0.36)),
    ("Issuer responded: do not honour",
     Truth(0.13, 0.48, 0.22)),
    ("Cardholder authentication unavailable at directory server",
     Truth(0.27, 0.46, 0.14, downtime_linked=True)),
    ("Account dormant, please contact the branch",
     Truth(0.01, 0.38, 0.43)),
    ("Beneficiary account frozen by order of the issuing institution",
     Truth(0.00, 0.05, 0.03)),
    ("NPCI switch declined with response code Z9",
     Truth(0.16, 0.53, 0.11, downtime_linked=True)),
    ("Per-transaction ceiling breached for the linked savings account",
     Truth(0.07, 0.56, 0.31, amount_sensitive=True)),
    ("Velocity check failed at issuer, temporary block applied",
     Truth(0.06, 0.24, 0.18)),
]
