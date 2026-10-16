"""Topic writes: create, update, and delete, each with its audit row.

Writes live here rather than in the route so the same three operations are reachable from a script
or the operations UI without going through HTTP. The route shapes a request and hands over a
mapping of the fields the caller actually sent; deciding what those fields mean is this module's
job, and the meaning does not change with the transport.

Four rules are worth stating, because each is a place where the obvious implementation is wrong:

* **A name is compared after trimming, and a nameless one is refused here.** Uniqueness is
  enforced by the database, so a name with spaces around it and the same name without would
  otherwise be two rows saying the same thing while the only place that checks uniqueness sees one.
  `normalize_name` is the one definition of the stored form and `require_usable_name` the one
  definition of an acceptable one, and the request boundary calls the same two functions — so the
  check and the write cannot disagree. The refusal is in this module rather than only at the
  boundary because the functions below are also how a script writes a topic, and a caller that
  never passes through a Pydantic model would otherwise store a row with no name.
* **A delete that history references is refused, not cascaded.** `agent_runs`, `pipeline_runs`,
  `reports` and `report_drafts` point at `topics.id` with `ON DELETE RESTRICT`, and they record
  what the topic produced. Disabling a topic that has a past is the operation the caller wants;
  deleting it would either fail at the database or, if the constraint were relaxed, delete the
  record of what happened.
* **A refused write is audited as well as a successful one.** Every refusal below writes a
  `failed` row before raising. "Who tried to take a name that was already in use" and "who tried to
  delete a topic that had produced reports" are questions only the refusals can answer; the trail
  that holds only successes cannot.
* **A refused create has no resource to name.** There is no topic yet, so `resource_id` is empty
  and the attempted name goes in `metadata_json` instead. That is the same asymmetry the
  authorization layer documents: a request-scoped refusal legitimately has nothing to point at,
  and inventing an id for it would make the trail lie.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final, NoReturn

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from open_source_intelligence.persistence.models.agent import AgentRun
from open_source_intelligence.persistence.models.intelligence import (
    PipelineRun,
    Topic,
    TopicRepository,
)
from open_source_intelligence.persistence.models.reporting import Report, ReportDraft
from open_source_intelligence.security.audit import record_audit_event

from .errors import TopicError, TopicInUse, TopicNameBlank, TopicNameConflict, TopicNotFound

#: API field name -> ORM attribute. The two list fields are stored with a `_json` suffix, which is
#: a storage detail rather than part of the field's meaning: the contract emits them as
#: `languages` / `keywords`, the same convention that turns the catalog's `topics_json` into
#: `topics` on egress. Keeping the mapping in one table means the route never has to know a column
#: name and this module never has to know an API name.
FIELD_COLUMNS: Final[dict[str, str]] = {
    "name": "name",
    "description": "description",
    "languages": "languages_json",
    "keywords": "keywords_json",
    "min_stars": "min_stars",
    "window_days": "window_days",
    "max_candidates": "max_candidates",
    "publish_mode": "publish_mode",
    "enabled": "enabled",
}

#: The tables that reference `topics.id`, in the order a refusal names them. Fixed rather than
#: discovered so that the same refusal reads the same way on every deployment, which is what makes
#: it worth writing into the contract's example.
REFERENCE_TABLES: Final[tuple[tuple[str, type[Any]], ...]] = (
    ("agent_runs", AgentRun),
    ("pipeline_runs", PipelineRun),
    ("reports", Report),
    ("report_drafts", ReportDraft),
)


def normalize_name(name: str) -> str:
    """Return the stored form of a topic name: surrounding whitespace removed.

    A pure normaliser: it answers "what would be stored", not "may this be stored". The refusal
    lives in `require_usable_name`, so a caller that only wants to compare names is not forced to
    handle an exception to do it.
    """
    return name.strip()


def require_usable_name(name: str) -> str:
    """Return the stored form of a name, refusing one that is blank once trimmed.

    Called from two places on purpose — the request boundary validates the `name` field with this
    function, and the writes below call it before storing. Splitting the two surfaces is how a
    topic with no name gets in: the endpoint is not the only writer (the module docstring says a
    script and the operations UI go through the same functions), and only one of those paths used
    to check.
    """
    normalized = normalize_name(name)
    if not normalized:
        raise TopicNameBlank("name must not be blank")
    return normalized


def _normalize_value(field: str, value: Any) -> Any:
    """Return the stored form of one incoming field value."""
    if field == "name":
        return require_usable_name(value)
    if field in ("languages", "keywords"):
        # An empty list and `null` mean the same thing to a topic filter ("no language/keyword
        # restriction"), so they are stored the same way. Collapsing them here is what keeps a
        # PATCH that clears the filter from looking like a change on every subsequent request.
        return [str(item).strip() for item in value] if value else None
    return value


def _stored_fields(values: Mapping[str, Any]) -> dict[str, Any]:
    """Translate incoming API fields into ORM attribute values for a constructor call."""
    translated: dict[str, Any] = {}
    for field, value in values.items():
        translated[_column_for(field)] = _normalize_value(field, value)
    return translated


def _column_for(field: str) -> str:
    """Return the ORM attribute for one API field, refusing a name the contract does not define."""
    column = FIELD_COLUMNS.get(field)
    if column is None:
        raise ValueError(f"unknown topic field {field!r}")
    return column


def _refuse(
    session: Session,
    *,
    action: str,
    actor_id: int,
    error: TopicError,
    request_id: str | None = None,
    resource_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> NoReturn:
    """Record a refused write, commit the trail, and re-raise.

    The commit is not an optimisation to avoid: the caller of this function is about to raise, and
    the session is closed on the way out. An audit row still in the session's pending state at that
    point is rolled back, which would leave every refusal invisible - exactly the failure this
    helper exists to prevent.

    `request_id` is threaded through rather than looked up, because this module has no request: the
    HTTP route has one and a script does not, and `None` from a script is the truthful answer. It
    matters because the trail's job is to answer "who tried this", and without the correlation id
    the row cannot be joined to the request log that recorded what they sent.
    """
    record_audit_event(
        session,
        action=action,
        resource_type="topic",
        resource_id=resource_id,
        result="failed",
        actor_id=actor_id,
        request_id=request_id,
        metadata_json=metadata,
    )
    session.commit()
    raise error


def _ensure_name_is_free(session: Session, name: str, *, keep_topic_id: int | None = None) -> None:
    """Refuse a name another topic already carries, optionally ignoring one row."""
    statement = select(Topic.id).where(Topic.name == name)
    if keep_topic_id is not None:
        statement = statement.where(Topic.id != keep_topic_id)
    if session.scalar(statement) is not None:
        raise TopicNameConflict(f"topic name {name!r} is already taken")


def reference_counts(session: Session, topic_id: int) -> dict[str, int]:
    """Count the rows in each referencing table that point at `topic_id`.

    Public because two callers need the same answer: the delete guard reads it to decide, and
    anything asking "why can this topic not be deleted" wants to show the same numbers rather than
    re-derive them.
    """
    counts: dict[str, int] = {}
    for label, model in REFERENCE_TABLES:
        counts[label] = (
            session.scalar(
                select(func.count()).select_from(model).where(model.topic_id == topic_id)
            )
            or 0
        )
    return counts


def create_topic(
    session: Session,
    *,
    actor_id: int,
    name: str,
    publish_mode: str,
    values: Mapping[str, Any] | None = None,
    request_id: str | None = None,
) -> Topic:
    """Create one topic and record the change.

    `name` and `publish_mode` are keyword arguments of their own because they are the two fields
    the table requires; everything else arrives in `values` only when the caller actually sent it,
    so the column defaults stay the single definition of what a new topic looks like. Repeating
    those defaults here is how a default ends up meaning two different things.

    `created_by` is the one required column with no default, so the actor is written into it
    rather than left to the database: the audit row and the row it describes then name the same
    account, which is what makes "who added this topic" answerable from either.
    """
    try:
        normalized = require_usable_name(name)
    except TopicNameBlank as error:
        _refuse(
            session,
            action="topic.create",
            actor_id=actor_id,
            error=error,
            request_id=request_id,
            metadata={"reason": error.code},
        )
    try:
        _ensure_name_is_free(session, normalized)
    except TopicNameConflict as error:
        _refuse(
            session,
            action="topic.create",
            actor_id=actor_id,
            error=error,
            request_id=request_id,
            metadata={"name": normalized, "reason": error.code},
        )
    topic = Topic(
        name=normalized,
        publish_mode=publish_mode,
        created_by=actor_id,
        **(_stored_fields(values) if values else {}),
    )
    session.add(topic)
    try:
        session.flush()
    except IntegrityError:
        # The uniqueness check above is not a lock, and two callers can pass it together. The
        # rollback is required before the audit row is added: the transaction that failed cannot
        # carry it.
        #
        # The re-check is what keeps this handler honest. "An `IntegrityError` means the name was
        # taken" was the first version, and it was wrong in the direction that hides bugs: a
        # missing required column also raises `IntegrityError`, so a broken insert was reported
        # as `409 TOPIC_NAME_CONFLICT` and the caller went off to pick a different name. Only a
        # name that is now genuinely taken is a conflict; anything else is re-raised and becomes
        # the `500` it is.
        session.rollback()
        try:
            _ensure_name_is_free(session, normalized)
        except TopicNameConflict:
            _refuse(
                session,
                action="topic.create",
                actor_id=actor_id,
                error=TopicNameConflict(f"topic name {normalized!r} is already taken"),
                request_id=request_id,
                metadata={"name": normalized, "reason": "TOPIC_NAME_CONFLICT", "race": True},
            )
        raise
    record_audit_event(
        session,
        action="topic.create",
        resource_type="topic",
        resource_id=str(topic.id),
        result="success",
        actor_id=actor_id,
        request_id=request_id,
        metadata_json={
            "name": topic.name,
            "publish_mode": topic.publish_mode,
            "enabled": topic.enabled,
        },
    )
    session.commit()
    return topic


def update_topic(
    session: Session,
    *,
    actor_id: int,
    topic_id: int,
    values: Mapping[str, Any],
    request_id: str | None = None,
) -> tuple[Topic, tuple[str, ...]]:
    """Apply the fields the caller sent and return the topic with the names of those changed.

    Only the fields present in `values` are touched, so `values` is what the request boundary got
    from `model_dump(exclude_unset=True)`: "not sent" and "sent as null" are different requests
    (`null` clears a description) and collapsing them would make one of the two impossible.

    A request whose fields all already hold the requested values is a success with an empty
    `changed` tuple, not a conflict. Reporting a no-op change as `409` would turn an idempotently
    repeated PATCH into something a client has to read an error to understand.

    `changed` names the **API** fields, not the columns: it is a description of the request, and a
    caller that sent `languages` should not be told it changed `languages_json`.
    """
    topic = session.get(Topic, topic_id)
    if topic is None:
        raise TopicNotFound(f"topic {topic_id} does not exist")
    changed: list[str] = []
    try:
        for field, raw in values.items():
            column = _column_for(field)
            value = _normalize_value(field, raw)
            if getattr(topic, column) != value:
                setattr(topic, column, value)
                changed.append(field)
    except TopicNameBlank as error:
        # Fields earlier in the same PATCH may already be staged on the instance; the rollback
        # discards them, so a refused request changes nothing rather than everything up to the
        # field that was refused.
        session.rollback()
        _refuse(
            session,
            action="topic.update",
            actor_id=actor_id,
            error=error,
            request_id=request_id,
            resource_id=str(topic_id),
            metadata={"reason": error.code},
        )
    if "name" in changed:
        # Captured as a plain string first: the rollback below expires `topic`, and reading
        # `topic.name` afterwards would report the name it still has in the database rather than
        # the one the caller was refused.
        attempted = topic.name
        try:
            _ensure_name_is_free(session, attempted, keep_topic_id=topic.id)
        except TopicNameConflict as error:
            # The rename is staged but not flushed. Committing the audit row without rolling back
            # first would carry the conflicting UPDATE along with it and fail on the unique
            # constraint, turning a named conflict into a 500.
            session.rollback()
            _refuse(
                session,
                action="topic.update",
                actor_id=actor_id,
                error=error,
                request_id=request_id,
                resource_id=str(topic_id),
                metadata={"name": attempted, "reason": error.code},
            )
    record_audit_event(
        session,
        action="topic.update",
        resource_type="topic",
        resource_id=str(topic.id),
        result="success",
        actor_id=actor_id,
        request_id=request_id,
        metadata_json={"changed_fields": changed},
    )
    session.commit()
    return topic, tuple(changed)


def delete_topic(
    session: Session,
    *,
    actor_id: int,
    topic_id: int,
    request_id: str | None = None,
) -> int:
    """Delete an unreferenced topic and return how many candidate links went with it.

    Refuses when any recording table references the topic, and audits the refusal. The linked
    `topic_repositories` rows are one ranking pass's output rather than history, so they are
    removed with the topic — the count is returned because deleting a topic and deleting its
    ranking are two effects, and the caller should learn both.
    """
    topic = session.get(Topic, topic_id)
    if topic is None:
        raise TopicNotFound(f"topic {topic_id} does not exist")
    counts = reference_counts(session, topic.id)
    blocking = [label for label, count in counts.items() if count]
    # Read the identifying values before the row is gone: after the commit they are unreachable.
    retained_id = topic.id
    retained_name = topic.name
    if blocking:
        _refuse(
            session,
            action="topic.delete",
            actor_id=actor_id,
            error=TopicInUse(f"topic {retained_id} is referenced by {', '.join(blocking)}"),
            request_id=request_id,
            resource_id=str(retained_id),
            metadata={"name": retained_name, "references": counts},
        )
    unlinked = (
        session.scalar(
            select(func.count())
            .select_from(TopicRepository)
            .where(TopicRepository.topic_id == retained_id)
        )
        or 0
    )
    session.delete(topic)
    record_audit_event(
        session,
        action="topic.delete",
        resource_type="topic",
        resource_id=str(retained_id),
        result="success",
        actor_id=actor_id,
        request_id=request_id,
        metadata_json={"name": retained_name, "unlinked_candidate_count": unlinked},
    )
    session.commit()
    return unlinked
