"""Stable report-push failures.

Guard violations raise; delivery failures are recorded on the `report_pushes` row and returned
as an outcome. The split is deliberate: "you may not push this" is a request error the caller
must fix, while "the transport refused it" is a retryable result that has to survive as
evidence. Every class carries a stable `code`, so the API can answer with a problem type and
the row can be retried without matching on message text.
"""


class PushError(ValueError):
    """Base class for every report-push failure."""

    code = "PUSH_ERROR"


class PushDisabled(PushError):
    """Raised when the deployment has switched pushing off entirely."""

    code = "PUSH_DISABLED"


class PushNotFound(PushError):
    """Raised when the addressed report or push record does not exist."""

    code = "NOT_FOUND"


class PushBlocked(PushError):
    """Raised when the report's status does not allow pushing.

    This is the guard behind "only approved reports may be pushed": the report has to have
    cleared review or actually been released, never merely been generated.
    """

    code = "PUSH_BLOCKED"


class PushTargetNotAllowed(PushError):
    """Raised when the destination is not an enabled, cleared entry of the allowlist."""

    code = "PUSH_TARGET_NOT_ALLOWED"


class PushApprovalRequired(PushError):
    """Raised when a confidential report has no human approval for the version being pushed."""

    code = "PUSH_APPROVAL_REQUIRED"


class PushDeliveryFailed(PushError):
    """Raised when a push cannot be attempted at all.

    A *transport* failure is not this: it is recorded as a failed attempt so it stays
    retryable. This class covers the cases where no attempt may be made — the retry budget is
    exhausted, or the channel is not configured for live delivery.
    """

    code = "PUSH_DELIVERY_FAILED"
