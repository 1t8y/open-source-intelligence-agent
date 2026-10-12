"""Tool execution errors mapped to stable contract error codes."""

from __future__ import annotations

from .contracts import RETRYABLE_ERROR_CODES


class ToolExecutionError(Exception):
    """Raised by a tool handler to return a specific contract error code."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    @property
    def retryable(self) -> bool:
        """Whether the caller may retry the same invocation."""
        return self.code in RETRYABLE_ERROR_CODES
