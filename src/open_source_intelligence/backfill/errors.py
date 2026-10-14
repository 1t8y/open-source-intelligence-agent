"""Domain errors for the resumable backfill."""


class BackfillError(RuntimeError):
    """Base error for a backfill that cannot proceed or could not finish."""


class BackfillJobNotFound(BackfillError):
    """Raised when a job id does not exist."""


class BackfillLimitError(BackfillError):
    """Raised when a job request is outside the bounds the schema accepts."""


class BackfillNotResumable(BackfillError):
    """Raised when a job has no pages left to walk.

    Not an error condition in the database sense - a finished job is the success case - but
    resuming one would either replay pages the cursor has already passed or claim progress
    the job never made, so the walk refuses rather than guessing which was meant.
    """


class BackfillUnsupportedCollector(BackfillError):
    """Raised when the selected source cannot be paged."""


class BackfillWalkFailed(BackfillError):
    """Raised after a walk has recorded its own failure.

    The job row and its run row already describe what happened by the time this is raised;
    the context is attached so a caller can report where the cursor stopped without having
    to re-read the database. The keywords default to empty values so Celery can rebuild the
    error from its JSON result backend, which carries only the message.
    """

    def __init__(
        self,
        message: str,
        *,
        job_id: int = 0,
        run_id: int = 0,
        error_code: str = "",
        next_page: int = 0,
    ) -> None:
        super().__init__(message)
        self.job_id = job_id
        self.run_id = run_id
        self.error_code = error_code
        self.next_page = next_page


class BackfillJobBusy(BackfillError):
    """Raised when another walk still owns the job's cursor."""

    def __init__(self, message: str, *, job_id: int, held_since: str) -> None:
        super().__init__(message)
        self.job_id = job_id
        self.held_since = held_since
