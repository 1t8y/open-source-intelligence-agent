"""Service-level tests for topic writes.

`topics/service.py` is the only writer of `topics`, and `test_topic_routes.py` exercises it through
the API. These tests pin the parts the API cannot reach or cannot tell apart:

* **A write that never passes through a Pydantic model is refused the same way.** The module's own
  docstring says the operations are reachable from a script and from the operations UI, and neither
  of those goes through the request boundary - so the trim rule and the blank-name rule have to
  hold here, not only there.
* **An `IntegrityError` is not automatically a name conflict.** The first version of this module
  caught every one and raised `TopicNameConflict`, which turns a broken insert into advice to pick
  a different name. The regression test below is the probe that found it.
* **"Cannot delete" is decided by counting, not by catching the database's refusal.** Which tables
  count is therefore a decision this module makes and a test has to state.

The three failures are told apart by `code`, because that is what `api/errors.py` maps to a status
and what a caller branches on.
"""

from __future__ import annotations

import pytest
from api_support import Actor, create_actor
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from support import Workspace

from open_source_intelligence.persistence.models.audit import AuditLog
from open_source_intelligence.persistence.models.intelligence import Topic, TopicRepository
from open_source_intelligence.topics import (
    TopicInUse,
    TopicNameBlank,
    TopicNameConflict,
    TopicNotFound,
    create_topic,
    delete_topic,
    reference_counts,
    update_topic,
)

#: The account fixture's username, kept here so two test modules in this package cannot collide
#: when they seed the same in-memory database shape.
ADMIN_USERNAME = "topic-service-admin"


@pytest.fixture()
def admin(session: Session) -> Actor:
    """The account whose `actor_id` every write here is attributed to."""
    return create_actor(session, username=ADMIN_USERNAME, role="ADMIN")


def _create(session: Session, admin: Actor, name: str, **values: object) -> Topic:
    """Create one topic through the service, with the calls the assertions need kept short."""
    return create_topic(
        session,
        actor_id=admin.user_id,
        name=name,
        publish_mode="MANUAL",
        values=values or None,
    )


def _rows(session: Session, action: str, result: str | None = None) -> list[AuditLog]:
    """Return the audit rows for one action, optionally of one result, in insertion order."""
    statement = select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
    if result is not None:
        statement = statement.where(AuditLog.result == result)
    return list(session.scalars(statement).all())


# --------------------------------------------------------------------------------------
# create
# --------------------------------------------------------------------------------------


def test_create_writes_the_row_and_its_audit(session: Session, admin: Actor) -> None:
    """A successful create leaves a row attributed to the actor and one `success` audit row."""
    topic = _create(session, admin, "vector-db")

    assert topic.id is not None
    assert topic.created_by == admin.user_id
    assert topic.enabled is True
    rows = _rows(session, "topic.create")
    assert len(rows) == 1
    assert rows[0].result == "success"
    assert rows[0].resource_type == "topic"
    assert rows[0].resource_id == str(topic.id)
    assert rows[0].actor_id == admin.user_id
    assert rows[0].metadata_json == {
        "name": "vector-db",
        "publish_mode": "MANUAL",
        "enabled": True,
    }


def test_create_attributes_the_row_to_the_actor(session: Session, admin: Actor) -> None:
    """`created_by` is the required column with no default, so the service has to write it.

    Leaving it to the database makes every insert fail, and because the failure is an
    `IntegrityError` the old handler reported it as a taken name. The assertion is separate from
    the audit one above so that a regression names the field that broke rather than the row count.
    """
    topic = _create(session, admin, "attributed")
    assert topic.created_by == admin.user_id


def test_a_direct_call_records_no_request_id(session: Session, admin: Actor) -> None:
    """A script has no request, so the column is null and that is the truthful value.

    The counterpart of the route test: `request_id` is threaded in by whoever has one rather than
    looked up, which is what keeps this module usable outside HTTP without inventing a correlation
    id that nothing else in the trail would share.
    """
    _create(session, admin, "no-request")
    assert _rows(session, "topic.create", "success")[0].request_id is None
    with pytest.raises(TopicNameConflict):
        _create(session, admin, "no-request")
    assert _rows(session, "topic.create", "failed")[0].request_id is None


def test_a_supplied_request_id_is_stored(session: Session, admin: Actor) -> None:
    """When the caller does have a correlation id, it reaches the row."""
    topic = create_topic(
        session,
        actor_id=admin.user_id,
        name="correlated",
        publish_mode="MANUAL",
        request_id="drill-0003",
    )
    update_topic(
        session,
        actor_id=admin.user_id,
        topic_id=topic.id,
        values={"enabled": False},
        request_id="drill-0003",
    )
    delete_topic(session, actor_id=admin.user_id, topic_id=topic.id, request_id="drill-0003")

    rows = [
        row
        for row in session.scalars(select(AuditLog).order_by(AuditLog.id)).all()
        if row.action.startswith("topic.")
    ]
    assert [row.action for row in rows] == ["topic.create", "topic.update", "topic.delete"]
    assert {row.request_id for row in rows} == {"drill-0003"}


def test_create_trims_the_name_before_it_stores_it(session: Session, admin: Actor) -> None:
    """The stored form is the trimmed form, which is what makes the uniqueness check meaningful."""
    assert _create(session, admin, "  向量数据库  ").name == "向量数据库"


def test_create_only_writes_the_values_the_caller_sent(session: Session, admin: Actor) -> None:
    """The column defaults stay the single definition of what a new topic looks like."""
    topic = _create(session, admin, "partial", min_stars=250, languages=["Rust"])

    assert topic.min_stars == 250
    assert topic.languages_json == ["Rust"]
    assert topic.window_days == 7
    assert topic.max_candidates == 30
    assert topic.description is None
    assert topic.keywords_json is None


def test_create_refuses_a_blank_name_and_names_nothing(session: Session, admin: Actor) -> None:
    """A name of spaces is a refusal, and there is no topic for the audit row to point at."""
    with pytest.raises(TopicNameBlank) as raised:
        _create(session, admin, "   ")

    assert raised.value.code == "TOPIC_NAME_BLANK"
    assert session.scalars(select(Topic)).all() == []
    rows = _rows(session, "topic.create")
    assert len(rows) == 1
    assert rows[0].result == "failed"
    assert rows[0].resource_id is None
    assert rows[0].metadata_json == {"reason": "TOPIC_NAME_BLANK"}


def test_create_refuses_a_taken_name_and_audits_the_refusal(session: Session, admin: Actor) -> None:
    """The refusal is in the trail: only it can answer "who else wanted this name"."""
    _create(session, admin, "taken")
    with pytest.raises(TopicNameConflict) as raised:
        _create(session, admin, "taken")

    assert raised.value.code == "TOPIC_NAME_CONFLICT"
    assert len(session.scalars(select(Topic).where(Topic.name == "taken")).all()) == 1
    failures = _rows(session, "topic.create", "failed")
    assert len(failures) == 1
    assert failures[0].metadata_json == {"name": "taken", "reason": "TOPIC_NAME_CONFLICT"}
    assert len(_rows(session, "topic.create", "success")) == 1


def test_create_does_not_relabel_an_unrelated_integrity_error(
    session: Session, admin: Actor
) -> None:
    """A constraint violation that is not the unique name is re-raised, not renamed.

    `publish_mode` carries a CHECK constraint, so a value outside the enum is a broken write rather
    than a taken name. The endpoint cannot produce this - the request model constrains the field -
    which is exactly why it matters here: the direct caller has no model to translate the answer,
    and "409 TOPIC_NAME_CONFLICT" sends it off to choose a different name for a problem that has
    nothing to do with names.
    """
    with pytest.raises(IntegrityError):
        create_topic(
            session,
            actor_id=admin.user_id,
            name="broken-mode",
            publish_mode="SOMETIMES",
        )

    assert session.scalars(select(Topic)).all() == []
    assert _rows(session, "topic.create") == []


# --------------------------------------------------------------------------------------
# update
# --------------------------------------------------------------------------------------


def test_update_applies_only_the_fields_it_was_given(session: Session, admin: Actor) -> None:
    """`values` is what the caller sent, and the fields it omits keep their values."""
    topic = _create(session, admin, "upd", description="before", min_stars=10)

    updated, changed = update_topic(
        session,
        actor_id=admin.user_id,
        topic_id=topic.id,
        values={"min_stars": 20},
    )

    assert changed == ("min_stars",)
    assert updated.min_stars == 20
    assert updated.description == "before"
    assert updated.name == "upd"


def test_update_names_the_api_field_and_not_the_column(session: Session, admin: Actor) -> None:
    """`changed_fields` describes the request; `languages_json` is a storage detail.

    The topic list's egress drops the `_json` suffix for the same reason, so a caller reading the
    audit row sees the field name it sent rather than one it never mentioned.
    """
    topic = _create(session, admin, "upd-names")

    updated, changed = update_topic(
        session,
        actor_id=admin.user_id,
        topic_id=topic.id,
        values={"languages": ["Go"]},
    )

    assert changed == ("languages",)
    assert updated.languages_json == ["Go"]
    rows = _rows(session, "topic.update")
    assert len(rows) == 1
    assert rows[0].result == "success"
    assert rows[0].resource_id == str(topic.id)
    assert rows[0].metadata_json == {"changed_fields": ["languages"]}


def test_update_of_an_unchanged_value_is_a_success_with_no_change(
    session: Session,
    admin: Actor,
) -> None:
    """A repeated PATCH is idempotent, not a conflict."""
    topic = _create(session, admin, "same")

    updated, changed = update_topic(
        session,
        actor_id=admin.user_id,
        topic_id=topic.id,
        values={"name": "same", "publish_mode": "MANUAL"},
    )

    assert changed == ()
    assert updated.name == "same"


def test_update_clears_a_list_with_an_empty_one(session: Session, admin: Actor) -> None:
    """Submitting `[]` and `null` mean the same thing, so the second PATCH changes nothing."""
    topic = _create(session, admin, "cleared", keywords=["agent", "llm"])

    updated, changed = update_topic(
        session,
        actor_id=admin.user_id,
        topic_id=topic.id,
        values={"keywords": []},
    )

    assert changed == ("keywords",)
    assert updated.keywords_json is None

    _, again = update_topic(
        session,
        actor_id=admin.user_id,
        topic_id=topic.id,
        values={"keywords": None},
    )
    assert again == ()


def test_update_rolls_the_whole_request_back_when_the_name_is_taken(
    session: Session,
    admin: Actor,
) -> None:
    """A refused PATCH changes nothing, including the fields that were fine on their own.

    The rename is checked after the loop, so `min_stars` is already staged on the instance when the
    conflict is found. Without the rollback it would be committed along with the refusal's audit
    row, and a request that got a `409` would have changed the row anyway.
    """
    _create(session, admin, "first")
    second = _create(session, admin, "second")

    with pytest.raises(TopicNameConflict):
        update_topic(
            session,
            actor_id=admin.user_id,
            topic_id=second.id,
            values={"name": "first", "min_stars": 999},
        )

    session.expire_all()
    reloaded = session.get(Topic, second.id)
    assert reloaded is not None
    assert reloaded.name == "second"
    assert reloaded.min_stars == 0
    failures = _rows(session, "topic.update", "failed")
    assert len(failures) == 1
    assert failures[0].resource_id == str(second.id)
    assert failures[0].metadata_json == {"name": "first", "reason": "TOPIC_NAME_CONFLICT"}


def test_update_refuses_a_blank_name(session: Session, admin: Actor) -> None:
    """A PATCH that would leave the topic nameless is refused, and the old name survives."""
    topic = _create(session, admin, "upd-blank")

    with pytest.raises(TopicNameBlank):
        update_topic(
            session,
            actor_id=admin.user_id,
            topic_id=topic.id,
            values={"name": "  "},
        )

    session.expire_all()
    reloaded = session.get(Topic, topic.id)
    assert reloaded is not None
    assert reloaded.name == "upd-blank"
    assert _rows(session, "topic.update", "failed")[0].resource_id == str(topic.id)


def test_update_of_a_missing_topic_is_not_found_and_unrecorded(
    session: Session,
    admin: Actor,
) -> None:
    """Nothing existed, so nothing was refused."""
    with pytest.raises(TopicNotFound):
        update_topic(session, actor_id=admin.user_id, topic_id=987654, values={"enabled": False})
    assert session.scalars(select(AuditLog)).all() == []


# --------------------------------------------------------------------------------------
# delete
# --------------------------------------------------------------------------------------


def test_reference_counts_names_every_recording_table(
    session: Session,
    workspace: Workspace,
) -> None:
    """The refusal's message is only useful if it is complete, and the order is part of it.

    The four labels are the four tables with a `RESTRICT` foreign key to `topics.id` that record
    what the topic produced. They are listed explicitly rather than discovered so that the same
    refusal reads the same way on every deployment, which is what makes it worth writing into the
    contract's example.
    """
    topic = workspace.add_topic(name="counted")
    session.commit()

    counts = reference_counts(session, topic.id)

    assert tuple(counts) == ("agent_runs", "pipeline_runs", "reports", "report_drafts")
    assert set(counts.values()) == {0}


def test_delete_refuses_a_topic_that_recorded_history(
    session: Session,
    workspace: Workspace,
    admin: Actor,
) -> None:
    """A topic with a past is disabled, not deleted, and the refusal says which tables hold it."""
    topic = workspace.add_topic(name="has-history")
    workspace.add_agent_run(topic.id)
    session.commit()

    with pytest.raises(TopicInUse) as raised:
        delete_topic(session, actor_id=admin.user_id, topic_id=topic.id)

    assert raised.value.code == "TOPIC_IN_USE"
    message = str(raised.value)
    assert "agent_runs" in message
    assert "pipeline_runs" in message
    assert session.get(Topic, topic.id) is not None
    failures = _rows(session, "topic.delete", "failed")
    assert len(failures) == 1
    assert failures[0].resource_id == str(topic.id)
    assert failures[0].metadata_json["name"] == "has-history"
    assert failures[0].metadata_json["references"] == {
        "agent_runs": 1,
        "pipeline_runs": 1,
        "reports": 0,
        "report_drafts": 0,
    }


def test_delete_removes_an_unreferenced_topic_and_reports_the_links_it_took(
    session: Session,
    workspace: Workspace,
    admin: Actor,
) -> None:
    """Deleting a topic and deleting its ranking are two effects, so both are reported.

    The candidate links are one ranking pass's output rather than history, so they go with the
    topic. The count is what tells the caller that happened, and the link rows have to be gone:
    a returned count of 1 with the row still present would be a number nobody could act on.
    """
    topic = workspace.add_topic(name="no-history")
    source = workspace.add_source()
    repository = workspace.add_repository(source_id=source.id)
    workspace.link(topic.id, repository.id)
    session.commit()
    topic_id = topic.id

    unlinked = delete_topic(session, actor_id=admin.user_id, topic_id=topic_id)

    assert unlinked == 1
    assert session.get(Topic, topic_id) is None
    assert (
        session.scalars(select(TopicRepository).where(TopicRepository.topic_id == topic_id)).all()
        == []
    )
    success = _rows(session, "topic.delete", "success")
    assert len(success) == 1
    assert success[0].metadata_json == {"name": "no-history", "unlinked_candidate_count": 1}


def test_delete_of_a_missing_topic_is_not_found_and_unrecorded(
    session: Session,
    admin: Actor,
) -> None:
    """Same as the update case: a miss is not a refusal."""
    with pytest.raises(TopicNotFound):
        delete_topic(session, actor_id=admin.user_id, topic_id=987654)
    assert session.scalars(select(AuditLog)).all() == []
