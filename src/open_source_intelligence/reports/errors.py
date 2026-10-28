"""Stable report-generation and publishing failures."""


class ReportGenerationError(ValueError):
    """Raised when a draft cannot be promoted into a traceable report."""

    #: Stable identifier so a caller (the API, the CLI, the worker) can branch on the failure
    #: instead of matching on the message. It used to be absent, and callers printed `null`.
    code = "REPORT_GENERATION_FAILED"


class ReportNotFoundError(ReportGenerationError):
    """Raised when the addressed report or report version does not exist.

    It is separate from the generic generation failure so the API can answer `404` for a
    missing resource and `400` for a rejected request without matching on message text.
    """

    code = "NOT_FOUND"


class ReportPublishError(ValueError):
    """Raised when a report cannot reach the requested publishing state.

    Every subclass carries a stable `code` so a blocked release can be recorded on
    `pipeline_runs.error_code` without matching on message text.
    """

    code = "PUBLISH_ERROR"


class ReportStateError(ReportPublishError):
    """Raised when a transition is not allowed from the report's current status."""

    code = "REPORT_STATE_INVALID"


class PublishPolicyBlocked(ReportPublishError):
    """Raised when publishing would violate the publish policy.

    This is the `INV-5` guard: a report may only be published after a verification verdict
    exists for its current version, and a `MANUAL` report additionally needs an approval.
    """

    code = "PUBLISH_POLICY_BLOCKED"
