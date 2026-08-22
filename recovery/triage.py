"""
Failure triage: map a failed payment onto a recovery archetype.

Deliberately two-tier, and the split is the point:

  Tier 1 - rules.  Every catalogued error code routes deterministically. This
           is ~94% of traffic. It is free, instant, auditable, and does not
           need a model. Using an LLM here would be worse in every dimension.

  Tier 2 - model.  Free-text acquirer strings with no catalogued code have no
           deterministic route. A small classifier call maps them into the same
           five archetypes under a constrained schema.

`evaluate.py` scores the tiers separately so the model's contribution is a
number rather than a claim. If tier 2 ever stops beating its keyword fallback,
delete it.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from .error_codes import ARCHETYPES, ARCHETYPE_HELP, Archetype

# Deterministic routing table for catalogued codes.
RULES: dict[str, Archetype] = {
    "GATEWAY_ERROR": "RETRY_AFTER_WINDOW",
    "GATEWAY_ERROR:issuer_unavailable": "RETRY_AFTER_WINDOW",
    "BAD_REQUEST_ERROR:mandate_not_active": "RETRY_AFTER_WINDOW",
    "BAD_REQUEST_ERROR:payment_timeout": "RETRY_NOW",
    "BAD_REQUEST_ERROR:invalid_otp": "RETRY_NOW",
    "BAD_REQUEST_ERROR:upi_collect_declined": "RETRY_NOW",
    "BAD_REQUEST_ERROR:authentication_failed": "SWITCH_RAIL",
    "BAD_REQUEST_ERROR:card_declined": "SWITCH_RAIL",
    "BAD_REQUEST_ERROR:limit_exceeded": "SWITCH_RAIL",
    "BAD_REQUEST_ERROR:insufficient_funds": "CUSTOMER_ACTION",
    "BAD_REQUEST_ERROR:card_expired": "CUSTOMER_ACTION",
    "BAD_REQUEST_ERROR:invalid_vpa": "CUSTOMER_ACTION",
    "BAD_REQUEST_ERROR:mandate_revoked": "CUSTOMER_ACTION",
    "BAD_REQUEST_ERROR:payment_declined_risk": "UNRECOVERABLE",
}

# Keyword fallback for tier 2 when no API key is configured, or the call fails.
# Also the control arm the model is measured against.
FALLBACK_PATTERNS: list[tuple[str, Archetype]] = [
    (r"frozen|dormant|do not honour|blocked|fraud", "UNRECOVERABLE"),
    (r"not respond|unavailable|switch declined|routing|timeout|interval", "RETRY_AFTER_WINDOW"),
    (r"ceiling|limit|not permitted|account type", "SWITCH_RAIL"),
    (r"velocity|temporary block", "CUSTOMER_ACTION"),
]

SYSTEM_PROMPT = f"""You classify failed payment errors from an Indian payment gateway into recovery archetypes.

Archetypes:
{chr(10).join(f"- {a}: {ARCHETYPE_HELP[a]}" for a in ARCHETYPES)}

Rules:
- Issuer/PSP/switch unresponsive or degraded -> RETRY_AFTER_WINDOW.
- Rail-specific restriction that will recur on the same rail -> SWITCH_RAIL.
- Requires the customer to change something (balance, card, VPA, mandate) -> CUSTOMER_ACTION.
- Account frozen, blocked, dormant, or a hard risk decline -> UNRECOVERABLE.
- Genuinely transient with no rail restriction -> RETRY_NOW.

Respond with JSON only. No prose, no markdown fences.
Schema: {{"archetype": <one of the five>, "confidence": <0.0-1.0>, "reason": <max 15 words>}}"""


@dataclass
class Triage:
    archetype: Archetype
    confidence: float
    reason: str
    tier: str            # "rules" | "model" | "fallback"
    model_cost_usd: float = 0.0
    latency_ms: float = 0.0


def _fallback(description: str) -> Triage:
    text = description.lower()
    for pattern, archetype in FALLBACK_PATTERNS:
        if re.search(pattern, text):
            return Triage(archetype, 0.45, "keyword match on acquirer text", "fallback")
    return Triage("RETRY_NOW", 0.30, "no pattern matched, default single retry", "fallback")


class Triager:
    """Holds the optional Anthropic client and a cache keyed on error text."""

    MODEL = "claude-sonnet-4-6"
    # Published per-million-token rates for the configured model.
    IN_RATE, OUT_RATE = 3.00 / 1e6, 15.00 / 1e6

    def __init__(self, use_model: bool | None = None):
        self._cache: dict[str, Triage] = {}
        self._client = None
        key = os.environ.get("ANTHROPIC_API_KEY")
        self.use_model = bool(key) if use_model is None else (use_model and bool(key))
        if self.use_model:
            try:
                import anthropic

                self._client = anthropic.Anthropic(api_key=key)
            except Exception:
                self.use_model = False

    def classify(self, payment: dict) -> Triage:
        code = payment.get("error_code")
        if code and code in RULES:
            return Triage(RULES[code], 0.97, f"catalogued code {code}", "rules")

        description = payment.get("error_description", "")
        if description in self._cache:
            return self._cache[description]

        result = self._classify_uncatalogued(description, payment)
        self._cache[description] = result
        return result

    def _classify_uncatalogued(self, description: str, payment: dict) -> Triage:
        if not self.use_model or self._client is None:
            return _fallback(description)

        import time

        started = time.perf_counter()
        try:
            response = self._client.messages.create(
                model=self.MODEL,
                max_tokens=150,
                system=SYSTEM_PROMPT,
                messages=[{
                    "role": "user",
                    "content": json.dumps({
                        "error_text": description,
                        "method": payment.get("method"),
                        "step": payment.get("error_step"),
                        "source": payment.get("error_source"),
                    }),
                }],
            )
            raw = "".join(b.text for b in response.content if b.type == "text")
            parsed = json.loads(raw.replace("```json", "").replace("```", "").strip())

            archetype = parsed.get("archetype")
            if archetype not in ARCHETYPES:
                return _fallback(description)

            usage = response.usage
            cost = usage.input_tokens * self.IN_RATE + usage.output_tokens * self.OUT_RATE
            return Triage(
                archetype=archetype,
                confidence=float(parsed.get("confidence", 0.6)),
                reason=str(parsed.get("reason", ""))[:80],
                tier="model",
                model_cost_usd=cost,
                latency_ms=(time.perf_counter() - started) * 1000,
            )
        except Exception:
            # Any model failure degrades to the deterministic path rather than
            # dropping the payment. Recovery must not depend on model uptime.
            return _fallback(description)
