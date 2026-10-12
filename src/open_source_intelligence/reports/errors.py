"""Stable report-generation failures."""


class ReportGenerationError(ValueError):
    """Raised when a draft cannot be promoted into a traceable report."""
