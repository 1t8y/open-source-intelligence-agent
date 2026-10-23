"""Stable error codes raised by model provider adapters."""

from __future__ import annotations

from decimal import Decimal

MODEL_REQUEST_FAILED = "MODEL_REQUEST_FAILED"
MODEL_TIMEOUT = "MODEL_TIMEOUT"
MODEL_RATE_LIMITED = "MODEL_RATE_LIMITED"
MODEL_INVALID_RESPONSE = "MODEL_INVALID_RESPONSE"

MODEL_ERROR_CODES = (
    MODEL_REQUEST_FAILED,
    MODEL_TIMEOUT,
    MODEL_RATE_LIMITED,
    MODEL_INVALID_RESPONSE,
)

RETRYABLE_MODEL_ERRORS = frozenset({MODEL_TIMEOUT, MODEL_RATE_LIMITED})


class ModelClientError(Exception):
    """A provider failure carrying a stable code instead of a vendor message.

    `prompt_tokens`, `completion_tokens`, and `cost` carry the usage the provider already
    billed for a request that failed *after* the response arrived (a truncated answer, an
    unparsable body). A call that was paid for is a call that has to be accounted for, so the
    graph adds these to the run totals instead of letting the budget walk backwards.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost: Decimal = Decimal("0"),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.prompt_tokens = max(0, prompt_tokens)
        self.completion_tokens = max(0, completion_tokens)
        self.cost = cost

    @property
    def retryable(self) -> bool:
        """Return whether the caller may safely retry the same request."""
        return self.code in RETRYABLE_MODEL_ERRORS
