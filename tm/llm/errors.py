"""Typed exceptions for the LLM client foundation.

Adapters and the cost meter raise these so callers can react with structured
error handling rather than parsing string messages. The hierarchy is shallow
on purpose: keep failure modes obvious.
"""

from __future__ import annotations

__all__ = [
    "CostCapExceeded",
    "LLMClientError",
]


class LLMClientError(RuntimeError):
    """Base class for LLM-client foundation errors.

    Raised for adapter-level problems such as missing API keys, malformed
    upstream responses, or other unrecoverable client-side conditions.
    """


class CostCapExceeded(LLMClientError):
    """Raised by ``CostMeter.check_budget`` when the pre-call estimate would
    push the running monthly total past the configured cap.

    Attributes:
        monthly_total_usd: running monthly spend (USD) at the time of the check.
        estimate_usd: estimated cost (USD) of the proposed call.
        cap_usd: configured monthly cap (USD).
    """

    def __init__(
        self,
        *,
        monthly_total_usd: float,
        estimate_usd: float,
        cap_usd: float,
    ) -> None:
        super().__init__(
            f"monthly LLM cost cap exceeded: "
            f"total={monthly_total_usd:.4f} + estimate={estimate_usd:.4f} "
            f"> cap={cap_usd:.4f} USD"
        )
        self.monthly_total_usd = monthly_total_usd
        self.estimate_usd = estimate_usd
        self.cap_usd = cap_usd
