"""Observability failures surfaced through the API as Problem Details.

The overview itself cannot fail a business rule - it only reads what other layers wrote. The one
thing that can go wrong is asking for a run that is not there, so this hierarchy stays small and
reuses the codes the rest of the API already emits.
"""

from __future__ import annotations


class ObservabilityError(Exception):
    """Base class for overview and run-ledger failures."""

    code: str = "OBSERVABILITY_ERROR"


class AgentRunNotFound(ObservabilityError):
    """Raised when an Agent run or its trajectory is requested but absent."""

    code = "NOT_FOUND"


__all__ = ["AgentRunNotFound", "ObservabilityError"]
