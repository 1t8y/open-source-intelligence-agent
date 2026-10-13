"""Stable report-generation and publishing failures."""


class ReportGenerationError(ValueError):
    """Raised when a draft cannot be promoted into a traceable report."""


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
