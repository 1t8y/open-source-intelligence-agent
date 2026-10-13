"""Stable error codes for the evaluation layer.

`api/errors.py` maps these onto Problem Details status codes, and the CLI prints `code`
instead of a message, so a script can branch on the failure without parsing English. The
`NOT_FOUND` code is shared by every missing-row case because the API renders all of them as
`404` and the resource name is already in the message.
"""

from __future__ import annotations


class EvaluationError(Exception):
    """Base class for every evaluation failure that carries a stable code."""

    code: str = "EVALUATION_ERROR"


class DatasetInvalid(EvaluationError):
    """A dataset file is malformed, incomplete, or internally inconsistent."""

    code = "DATASET_INVALID"


class DatasetVersionConflict(EvaluationError):
    """A dataset version already exists with different content.

    Versions are immutable, so this is refused rather than merged: silently accepting a change
    under an existing version would make every historical result unreproducible.
    """

    code = "DATASET_VERSION_CONFLICT"


class EvaluationNotFound(EvaluationError):
    """A dataset, case, or run does not exist."""

    code = "NOT_FOUND"


class UnknownTarget(EvaluationError):
    """The requested target variant is not one of the five defined variants."""

    code = "UNKNOWN_TARGET"


class EvaluationDisabled(EvaluationError):
    """Evaluation is switched off by configuration."""

    code = "EVALUATION_DISABLED"
