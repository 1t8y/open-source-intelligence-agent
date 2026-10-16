"""HTTP tests for the topic endpoints.

`docs/api-contract.md` §3.2 is the contract under test. Two of its properties get more attention
than the rest, because they are the two it argues for at length and the two a route can break
without any single response looking wrong:

* **The visibility rule reaches the database.** A list that fetched every topic and dropped the
  invisible ones in Python would return the right `items` and a `total` counting topics the caller
  may not see. Only the `total` would give it away, so the `total` is asserted on its own.
* **`q` is a substring search, not a pattern.** `%` and `_` are ordinary characters to the caller,
  so the endpoint must not hand them to `LIKE` unescaped - `q=_` would otherwise match every
  non-empty row.

The rest of the file is the role matrix, the two failure shapes the contract spells out (`403` with
no metadata plus an audit row naming the topic, `404` for a miss), and the deliberate absence of a
run trigger.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from api_support import Actor, auth_headers, build_client, create_actor
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from support import Workspace

from open_source_intelligence.persistence.models.audit import AuditLog
from open_source_intelligence.persistence.models.intelligence import Topic

TOPICS = "/api/v1/topics"

#: The account names each fixture creates. Distinct from the service tests' fixture so the two
#: modules cannot collide on a username if they ever share a database shape.
USERNAMES = {
    "admin": "topics-route-admin",
    "editor": "topics-route-editor",
    "viewer": "topics-route-viewer",
}

#: Every role the deployment can produce, in the order the matrix below walks them.
READING_ROLES = ("ADMIN", "EDITOR", "VIEWER")


@pytest.fixture()
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    """Provide a client bound to the shared test database."""
    with build_client(session_factory) as started:
        yield started


def _actor(session: Session, key: str, role: str) -> Actor:
    """Create the account a role fixture hands out."""
    return create_actor(session, username=USERNAMES[key], role=role)


@pytest.fixture()
def admin(session: Session) -> Actor:
    """An administrator: the only role allowed to write a topic."""
    return _actor(session, "admin", "ADMIN")


@pytest.fixture()
def editor(session: Session) -> Actor:
    """An editor: may write reports, may not write topics."""
    return _actor(session, "editor", "EDITOR")


@pytest.fixture()
def viewer(session: Session) -> Actor:
    """A read-only account."""
    return _actor(session, "viewer", "VIEWER")


def _payload(**fields: object) -> dict[str, object]:
    """Build a create/update body from the two required fields plus whatever was passed.

    Keyword-only so that a caller overriding `name` (the bounds test does) supplies it the same way
    every other field arrives, instead of colliding with a positional parameter.
    """
    body: dict[str, object] = {"name": "topic", "publish_mode": "MANUAL"}
    body.update(fields)
    return body


def _audit(session: Session, action: str, result: str | None = None) -> list[AuditLog]:
    """Return the audit rows for one action, optionally of one result, in insertion order."""
    statement = select(AuditLog).where(AuditLog.action == action).order_by(AuditLog.id)
    if result is not None:
        statement = statement.where(AuditLog.result == result)
    return list(session.scalars(statement).all())


def _create(client: TestClient, headers: dict[str, str], name: str, **overrides: object) -> int:
    """Create one topic through the API and return its id, failing loudly if it was not created."""
    response = client.post(TOPICS, json=_payload(name=name, **overrides), headers=headers)
    assert response.status_code == 201, response.text
    return int(response.json()["id"])


# --------------------------------------------------------------------------------------
# Authentication and the role matrix
# --------------------------------------------------------------------------------------


def test_the_list_refuses_an_anonymous_caller(client: TestClient) -> None:
    """Reading topics is not public: it needs an account, and the refusal says which problem it
    is."""
    response = client.get(TOPICS)
    assert response.status_code == 401
    assert response.json()["code"] == "AUTH_REQUIRED"


@pytest.mark.parametrize("role", READING_ROLES)
def test_every_standard_role_may_read_the_list(
    client: TestClient,
    session: Session,
    role: str,
) -> None:
    """The read side is not a privilege: the catalog these topics slice is itself readable."""
    actor = create_actor(session, username=f"route-reader-{role}", role=role)
    response = client.get(TOPICS, headers=auth_headers(client, actor))
    assert response.status_code == 200, response.text


def test_the_write_endpoints_refuse_a_viewer_and_an_editor(
    client: TestClient,
    session: Session,
    workspace: Workspace,
    viewer: Actor,
    editor: Actor,
) -> None:
    """`EDITOR`'s write permission covers reports and nothing else, and every refusal is audited.

    All three verbs are checked for both roles in one test because the point is that the same
    decision covers the whole group: a write route added later without the `ADMIN` dependency would
    fail here rather than in production.
    """
    topic = workspace.add_topic(name="seeded")
    session.commit()

    for actor in (viewer, editor):
        headers = auth_headers(client, actor)
        assert (
            client.post(TOPICS, json=_payload(name="refused"), headers=headers).status_code == 403
        )
        assert (
            client.patch(
                f"{TOPICS}/{topic.id}", json={"enabled": False}, headers=headers
            ).status_code
            == 403
        )
        assert client.delete(f"{TOPICS}/{topic.id}", headers=headers).status_code == 403

    denials = _audit(session, "auth.rbac")
    assert len(denials) == 6
    assert {row.result for row in denials} == {"denied"}
    assert {row.actor_id for row in denials} == {viewer.user_id, editor.user_id}
    # The refusal is request-scoped: the caller was turned away before the topic was loaded, so
    # there is no resource to name and inventing one would make the trail claim a lookup happened.
    assert {row.resource_id for row in denials} == {None}


# --------------------------------------------------------------------------------------
# The full write path
# --------------------------------------------------------------------------------------


def test_an_admin_creates_reads_updates_and_deletes(
    client: TestClient,
    session: Session,
    admin: Actor,
) -> None:
    """One walk of the whole lifecycle, with the response shapes the contract shows."""
    headers = auth_headers(client, admin)
    topic_id = _create(
        client,
        headers,
        "created-by-admin",
        description="关注 ANN 索引与检索质量",
        languages=["Rust", "C++"],
        keywords=["vector", "ann"],
        min_stars=200,
        window_days=14,
        max_candidates=20,
    )

    created = client.get(f"{TOPICS}/{topic_id}", headers=headers)
    assert created.status_code == 200
    body = created.json()
    assert body["created_by"] == admin.user_id
    assert body["name"] == "created-by-admin"
    assert body["languages"] == ["Rust", "C++"]
    assert body["keywords"] == ["vector", "ann"]
    assert body["min_stars"] == 200
    assert body["window_days"] == 14
    assert body["max_candidates"] == 20
    assert body["publish_mode"] == "MANUAL"
    assert body["enabled"] is True

    patched = client.patch(
        f"{TOPICS}/{topic_id}",
        json={"enabled": False, "min_stars": 500},
        headers=headers,
    )
    assert patched.status_code == 200
    assert patched.json()["enabled"] is False
    assert patched.json()["min_stars"] == 500
    assert patched.json()["languages"] == ["Rust", "C++"]

    deleted = client.delete(f"{TOPICS}/{topic_id}", headers=headers)
    assert deleted.status_code == 200
    assert deleted.json() == {
        "topic_id": topic_id,
        "deleted": True,
        "unlinked_candidate_count": 0,
    }

    assert client.get(f"{TOPICS}/{topic_id}", headers=headers).status_code == 404

    # One `success` per verb, in order: the lifecycle is the trail, not just the last step.
    assert [row.result for row in _audit(session, "topic.create")] == ["success"]
    assert [row.result for row in _audit(session, "topic.update")] == ["success"]
    assert [row.result for row in _audit(session, "topic.delete")] == ["success"]


def test_the_list_element_and_the_single_read_have_the_same_shape(
    client: TestClient,
    admin: Actor,
) -> None:
    """The contract says the detail response is the list element; this is the assertion of it."""
    headers = auth_headers(client, admin)
    topic_id = _create(client, headers, "shaped", description="d")

    listed = client.get(TOPICS, headers=headers).json()["items"]
    detail = client.get(f"{TOPICS}/{topic_id}", headers=headers).json()

    assert len(listed) == 1
    assert set(listed[0]) == set(detail)
    for field in ("id", "name", "description", "enabled", "publish_mode", "created_by"):
        assert listed[0][field] == detail[field]


def test_every_write_records_the_request_that_caused_it(
    client: TestClient,
    session: Session,
    admin: Actor,
) -> None:
    """Each `topic.*` row carries the request correlation id the middleware assigned.

    This is the defect the real run found. The service writes its own audit rows, and it was
    writing them without the id, so a topic change could be joined to the actor but not to the
    request log that recorded what they sent - the one thing the id exists for. Every other
    route-originated row in the project carries it (`api/routes/auth.py`, `web/router.py`), which
    is what made the omission visible.

    All three verbs are asserted together because the id is threaded through three separate
    functions and a fix applied to one of them would look identical from the outside.
    """
    headers = {**auth_headers(client, admin), "X-Request-ID": "drill-0001"}
    topic_id = _create(client, headers, "correlated")
    assert (
        client.patch(f"{TOPICS}/{topic_id}", json={"enabled": False}, headers=headers).status_code
        == 200
    )
    assert client.delete(f"{TOPICS}/{topic_id}", headers=headers).status_code == 200

    rows = session.scalars(
        select(AuditLog).where(
            AuditLog.action.in_(("topic.create", "topic.update", "topic.delete"))
        )
    ).all()
    assert len(rows) == 3
    assert {row.request_id for row in rows} == {"drill-0001"}


def test_a_refused_write_records_the_request_too(
    client: TestClient,
    session: Session,
    admin: Actor,
) -> None:
    """A refusal is the row most worth correlating, so it carries the id as well.

    The test is separate from the one above because the refusal path goes through `_refuse` rather
    than through the success branch: a fix that only covered the happy path would pass the other
    test and fail here.
    """
    headers = {**auth_headers(client, admin), "X-Request-ID": "drill-0002"}
    _create(client, headers, "taken-name")
    response = client.post(TOPICS, json=_payload(name="taken-name"), headers=headers)

    assert response.status_code == 409
    failure = session.scalar(
        select(AuditLog).where(
            AuditLog.action == "topic.create",
            AuditLog.result == "failed",
        )
    )
    assert failure is not None
    assert failure.request_id == "drill-0002"
    assert failure.metadata_json is not None
    assert failure.metadata_json["reason"] == "TOPIC_NAME_CONFLICT"


# --------------------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------------------


def test_a_blank_name_is_refused_at_the_boundary_with_the_field_named(
    client: TestClient,
    admin: Actor,
) -> None:
    """The `422` names the path and the reason, which is what makes it actionable."""
    response = client.post(TOPICS, json=_payload(name="   "), headers=auth_headers(client, admin))
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "VALIDATION_ERROR"
    assert "name" in body["detail"]
    assert "blank" in body["detail"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"publish_mode": "SOMETIMES"},
        {"min_stars": -1},
        {"window_days": 0},
        {"max_candidates": 0},
        {"name": "x" * 129},
        {"languages": ["y" * 65]},
    ],
)
def test_a_field_outside_its_documented_bounds_is_a_validation_error(
    client: TestClient,
    admin: Actor,
    overrides: dict[str, object],
) -> None:
    """Each row is one clause of the contract's validation table.

    The `name` row passes its value through the same merge every other row does, so the case that
    overrides a required field exercises the request model instead of the helper.
    """
    response = client.post(TOPICS, json=_payload(**overrides), headers=auth_headers(client, admin))
    assert response.status_code == 422, response.text
    assert response.json()["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("params", [{"page": 0}, {"page_size": 0}, {"page_size": 101}])
def test_pagination_outside_its_bounds_is_a_validation_error(
    client: TestClient,
    admin: Actor,
    params: dict[str, int],
) -> None:
    """The bound is enforced by the query declaration, so it is a `422` and not a clamped page."""
    response = client.get(TOPICS, params=params, headers=auth_headers(client, admin))
    assert response.status_code == 422


def test_a_duplicate_name_is_a_conflict(client: TestClient, admin: Actor) -> None:
    headers = auth_headers(client, admin)
    _create(client, headers, "duplicate")
    response = client.post(TOPICS, json=_payload(name="duplicate"), headers=headers)
    assert response.status_code == 409
    assert response.json()["code"] == "TOPIC_NAME_CONFLICT"


def test_a_name_that_only_differs_by_whitespace_is_the_same_name(
    client: TestClient,
    admin: Actor,
) -> None:
    """Two rows saying the same thing while the uniqueness check sees one is the failure mode."""
    headers = auth_headers(client, admin)
    _create(client, headers, "向量数据库")
    response = client.post(TOPICS, json=_payload(name=" 向量数据库 "), headers=headers)
    assert response.status_code == 409
    assert response.json()["code"] == "TOPIC_NAME_CONFLICT"


def test_renaming_onto_a_taken_name_is_a_conflict_and_changes_nothing(
    client: TestClient,
    session: Session,
    admin: Actor,
) -> None:
    headers = auth_headers(client, admin)
    _create(client, headers, "rename-first")
    second = _create(client, headers, "rename-second", min_stars=5)

    response = client.patch(
        f"{TOPICS}/{second}",
        json={"name": "rename-first", "min_stars": 999},
        headers=headers,
    )

    assert response.status_code == 409
    assert response.json()["code"] == "TOPIC_NAME_CONFLICT"
    session.expire_all()
    reloaded = session.get(Topic, second)
    assert reloaded is not None
    assert reloaded.name == "rename-second"
    assert reloaded.min_stars == 5


def test_a_patch_that_changes_nothing_is_still_a_success(client: TestClient, admin: Actor) -> None:
    headers = auth_headers(client, admin)
    topic_id = _create(client, headers, "idempotent")
    response = client.patch(f"{TOPICS}/{topic_id}", json={"name": "idempotent"}, headers=headers)
    assert response.status_code == 200
    assert response.json()["name"] == "idempotent"


# --------------------------------------------------------------------------------------
# Visibility
# --------------------------------------------------------------------------------------


@pytest.fixture()
def mixed(session: Session, workspace: Workspace) -> dict[str, int]:
    """One enabled topic and one disabled one, with the ids the assertions compare against."""
    enabled = workspace.add_topic(name="visible-topic", enabled=True)
    disabled = workspace.add_topic(name="hidden-topic", enabled=False)
    session.commit()
    return {"enabled": enabled.id, "disabled": disabled.id}


def test_a_disabled_topic_is_absent_from_a_viewers_items_and_total(
    client: TestClient,
    viewer: Actor,
    mixed: dict[str, int],
) -> None:
    """The filter reaches the database, so the count cannot include what the caller may not see."""
    body = client.get(TOPICS, headers=auth_headers(client, viewer)).json()
    assert [item["id"] for item in body["items"]] == [mixed["enabled"]]
    assert body["total"] == 1


def test_an_admin_reads_the_disabled_topic_and_the_total_counts_it(
    client: TestClient,
    admin: Actor,
    mixed: dict[str, int],
) -> None:
    body = client.get(TOPICS, headers=auth_headers(client, admin)).json()
    assert {item["id"] for item in body["items"]} == set(mixed.values())
    assert body["total"] == 2


def test_a_disabled_topic_is_forbidden_not_missing(
    client: TestClient,
    viewer: Actor,
    mixed: dict[str, int],
) -> None:
    """An invisible topic that exists is a `403`; the contract splits that from a `404`."""
    response = client.get(f"{TOPICS}/{mixed['disabled']}", headers=auth_headers(client, viewer))
    assert response.status_code == 403
    assert response.json()["code"] == "FORBIDDEN"


def test_the_denial_says_nothing_about_the_topic_and_the_audit_row_names_it(
    client: TestClient,
    session: Session,
    viewer: Actor,
    mixed: dict[str, int],
) -> None:
    """The two faces of a refusal, asserted together because either alone is satisfiable.

    A body that leaked the name would be a disclosure; an audit row that omitted the id would be
    uninvestigable. Neither shows up as a wrong status code, so both are pinned here.
    """
    response = client.get(f"{TOPICS}/{mixed['disabled']}", headers=auth_headers(client, viewer))

    assert response.status_code == 403
    detail = response.json()["detail"]
    assert "hidden-topic" not in detail
    assert str(mixed["disabled"]) not in detail

    rows = _audit(session, "topic.read", "denied")
    assert len(rows) == 1
    assert rows[0].resource_type == "topic"
    assert rows[0].resource_id == str(mixed["disabled"])
    assert rows[0].actor_id == viewer.user_id


def test_a_permitted_read_writes_no_audit_row(
    client: TestClient,
    session: Session,
    viewer: Actor,
    mixed: dict[str, int],
) -> None:
    """The trail records refusals, not traffic."""
    response = client.get(f"{TOPICS}/{mixed['enabled']}", headers=auth_headers(client, viewer))
    assert response.status_code == 200
    assert _audit(session, "topic.read") == []


def test_the_enabled_filter_intersects_visibility_instead_of_replacing_it(
    client: TestClient,
    viewer: Actor,
    admin: Actor,
    mixed: dict[str, int],
) -> None:
    """`enabled=false` is a true answer for a viewer and a list for an admin.

    "There are no disabled topics you may see" answers the question that was asked, so it is an
    empty page rather than a `403`. Reporting it as a refusal would tell the caller that something
    exists that it is not allowed to know about.
    """
    viewer_body = client.get(f"{TOPICS}?enabled=false", headers=auth_headers(client, viewer)).json()
    assert viewer_body == {"items": [], "page": 1, "page_size": 20, "total": 0}

    admin_body = client.get(f"{TOPICS}?enabled=false", headers=auth_headers(client, admin)).json()
    assert [item["id"] for item in admin_body["items"]] == [mixed["disabled"]]
    assert admin_body["total"] == 1


# --------------------------------------------------------------------------------------
# The query parameter
# --------------------------------------------------------------------------------------


@pytest.fixture()
def searched(session: Session, workspace: Workspace) -> None:
    """Three names chosen so each metacharacter case has exactly one right answer."""
    workspace.add_topic(name="vector_db")
    # Matches `vector_db` only if `_` is treated as a wildcard, which is the failure being ruled
    # out: a substring search for `_` must not match a name without one.
    workspace.add_topic(name="vectorXdb")
    workspace.add_topic(name="covers 65% of cases")
    session.commit()


def _total(client: TestClient, headers: dict[str, str], query: str) -> int:
    """Return `total` for one `q`, reading the count rather than the items.

    The count is the assertion that matters: `items` can look right while `total` is computed
    under a different predicate, and the pair is only consistent if both come from one condition
    list - which is the property `queries.py` documents.
    """
    body = client.get(TOPICS, params={"q": query}, headers=headers).json()
    return int(body["total"])


def test_the_query_is_a_literal_substring_search(
    client: TestClient,
    admin: Actor,
    searched: None,
) -> None:
    """`%` and `_` are characters to the caller, so they must be characters to the database."""
    headers = auth_headers(client, admin)
    assert _total(client, headers, "vector") == 2
    assert _total(client, headers, "_") == 1
    assert _total(client, headers, "%") == 1
    assert _total(client, headers, "65%") == 1
    assert _total(client, headers, "65_") == 0
    assert _total(client, headers, "zzzznomatch") == 0


def test_the_query_is_case_insensitive(client: TestClient, admin: Actor, searched: None) -> None:
    headers = auth_headers(client, admin)
    assert _total(client, headers, "VECTOR") == 2


def test_the_query_searches_the_description_too(client: TestClient, admin: Actor) -> None:
    headers = auth_headers(client, admin)
    _create(client, headers, "described", description="attention is all you need")
    body = client.get(TOPICS, params={"q": "attention"}, headers=headers).json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "described"


# --------------------------------------------------------------------------------------
# Pagination and ordering
# --------------------------------------------------------------------------------------


def test_the_list_orders_by_id_and_pages_without_gaps(client: TestClient, admin: Actor) -> None:
    """`id` ascending is the order the daily pipeline reads enabled topics in."""
    headers = auth_headers(client, admin)
    created = [_create(client, headers, f"ordered-{index}") for index in range(3)]

    first = client.get(TOPICS, params={"page": 1, "page_size": 2}, headers=headers).json()
    second = client.get(TOPICS, params={"page": 2, "page_size": 2}, headers=headers).json()

    assert first["total"] == 3
    assert [item["id"] for item in first["items"]] == created[:2]
    assert [item["id"] for item in second["items"]] == created[2:]


# --------------------------------------------------------------------------------------
# Deletion and the resources that outlive a topic
# --------------------------------------------------------------------------------------


def test_deleting_a_topic_that_recorded_history_is_a_conflict(
    client: TestClient,
    session: Session,
    workspace: Workspace,
    admin: Actor,
) -> None:
    """The refusal names the tables, so the caller learns which relation to look at."""
    topic = workspace.add_topic(name="with-history")
    workspace.add_agent_run(topic.id)
    session.commit()

    response = client.delete(f"{TOPICS}/{topic.id}", headers=auth_headers(client, admin))

    assert response.status_code == 409
    assert response.json()["code"] == "TOPIC_IN_USE"
    assert "agent_runs" in response.json()["detail"]


@pytest.mark.parametrize("verbs", ["patch", "delete"])
def test_a_missing_topic_is_a_not_found(
    client: TestClient,
    admin: Actor,
    verbs: str,
) -> None:
    headers = auth_headers(client, admin)
    if verbs == "patch":
        response = client.patch(f"{TOPICS}/987654", json={"enabled": False}, headers=headers)
    else:
        response = client.delete(f"{TOPICS}/987654", headers=headers)
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


def test_triggering_a_research_run_is_not_reachable(
    client: TestClient,
    admin: Actor,
) -> None:
    """§3.2 keeps the run trigger off the API; the route must not appear by accident.

    A run spends model budget, and the trigger belongs to the scheduler and the operator CLI. The
    assertion is that no route answers the path at all, which also catches a future edit that adds
    one without revisiting the decision.
    """
    headers = auth_headers(client, admin)
    topic_id = _create(client, headers, "no-run-trigger")
    assert client.post(f"{TOPICS}/{topic_id}/run", headers=headers).status_code == 404
