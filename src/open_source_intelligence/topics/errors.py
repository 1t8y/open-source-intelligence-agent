"""Stable topic-configuration failures.

Every class carries a stable `code`, so `api/errors.py` can render a Problem Details body and a
caller can branch on the code instead of parsing English. The three failures are genuinely
different answers: a name that is taken is something the caller can resolve by choosing another,
a topic that history references cannot be deleted at all, and a topic that is not there has no
answer to give.
"""


class TopicError(ValueError):
    """Base class for every topic-configuration failure."""

    code = "TOPIC_ERROR"


class TopicNotFound(TopicError):
    """Raised when the addressed topic does not exist.

    Only the direct-caller path raises this. The API reads a topic through
    `security/authorization.py:authorize_topic_read`, which raises its own `404` so that the
    existence check and the visibility check cannot be pulled apart.
    """

    code = "NOT_FOUND"


class TopicNameBlank(TopicError):
    """Raised when a topic name is empty once surrounding whitespace is removed.

    `ValueError` is the base class on purpose. The HTTP boundary validates the name field with a
    Pydantic `AfterValidator` that calls `service.require_usable_name`, and Pydantic turns a
    `ValueError` into a `422` naming the field; the same function guards the write itself, so a
    script or the operations UI gets the identical refusal instead of a nameless row. One check,
    two surfaces.
    """

    code = "TOPIC_NAME_BLANK"


class TopicNameConflict(TopicError):
    """Raised when another topic already carries the requested name.

    Uniqueness lives on the database. This class turns that constraint into a named conflict
    instead of an `IntegrityError`: two callers racing on the same name must both get an answer
    they can act on, and a 500 from a violated constraint is not one.
    """

    code = "TOPIC_NAME_CONFLICT"


class TopicInUse(TopicError):
    """Raised when a topic cannot be deleted because recorded history references it.

    `agent_runs`, `pipeline_runs`, `reports` and `report_drafts` all point at `topics.id` with
    `ON DELETE RESTRICT`, and they record what the topic produced. The message names the tables
    that hold the reference, because "it is in use" without saying where leaves the caller to
    guess among four.
    """

    code = "TOPIC_IN_USE"
