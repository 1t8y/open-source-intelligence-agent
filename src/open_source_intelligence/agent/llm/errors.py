"""Stable error codes raised by model provider adapters."""

from __future__ import annotations

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
    """A provider failure carrying a stable code instead of a vendor message."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    @property
    def retryable(self) -> bool:
        """Return whether the caller may safely retry the same request."""
        return self.code in RETRYABLE_MODEL_ERRORS
