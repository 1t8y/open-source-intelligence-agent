"""Run the T18 documentation-sync smoke against a real database.

T18's acceptance is "架构、API、数据模型和评测文档与代码一致", and it earned that name by
turning up four API resource groups the contract had declared and the code had never
implemented. Closing that gap was half of the task. This script is the evidence for the other
half, which is that the two groups it kept (`FR-D-05` names 报告、仓库、Agent、评测和审计 API)
actually work now:

1. **The repository catalog** - the list and the trend series answer over real rows, the rank
   fields say "never ranked" instead of zero when no topic is named, a disabled topic is refused
   to a viewer and served to an administrator, and the trend window is closed on both ends.
2. **The audit trail** - a non-administrator is refused, the refusal is itself in the ledger, the
   trail comes back newest-first, and `metadata_json` does not leave the process.
3. **The boundary fix** - a timestamp without a timezone offset is a `422` at the edge rather than
   a `500` from inside the query builder. That defect was latent in `GET /reports` since T12 and
   was only found because T18 read the documents against the code.
4. **The drift checker** - the ten documentation comparisons run in-process, so one command
   produces both the runtime evidence and the documentary verdict.

Nothing here re-implements the code under test. The API is the real ASGI application in process,
the rows are written by the real security service, and the catalog is read through the shipped
`pipeline.queries` layer.
"""

from __future__ import annotations

import argparse
import json
from datetime import date, timedelta
from decimal import Decimal
from typing import Any
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session, sessionmaker

from open_source_intelligence.api.app import create_app
from open_source_intelligence.core.config import Settings, get_settings
from open_source_intelligence.persistence import models  # noqa: F401
from open_source_intelligence.persistence.base import utc_now
from open_source_intelligence.persistence.models.audit import AuditLog
from open_source_intelligence.persistence.models.identity import Role, User, UserRole
from open_source_intelligence.persistence.models.intelligence import (
    DataSource,
    Repository,
    RepoSnapshot,
    Topic,
    TopicRepository,
)
from open_source_intelligence.persistence.session import (
    create_database_engine,
    create_session_factory,
)
from open_source_intelligence.security.audit import record_audit_event
from open_source_intelligence.security.passwords import hash_password
from open_source_intelligence.security.service import create_user

# --------------------------------------------------------------------------------------
# Drill-owned names, dates, and sentinels
# --------------------------------------------------------------------------------------

#: Every row this drill creates carries one of these prefixes, so the cleanup pass can find its
#: own work by name instead of by an id watermark. Name-based cleanup is what makes the drill
#: re-runnable: a leftover row from an interrupted run is removed by the next one rather than
#: being counted.
NAME_PREFIX = "__t18_drill__"
ACTOR_PREFIX = "__t18_"
#: The `X-Request-ID` every drill request carries. The middleware copies an incoming header into
#: `request.state.request_id`, and `audit_logs.request_id` is written from it, so this is what
#: makes the drill's *anonymous* audit rows - a 401 writes a row with `actor_id` NULL, which no
#: actor-based purge can ever reach - identifiable and removable. Without it every run of this
#: script left one `auth.token` failure in an append-only ledger permanently.
REQUEST_ID_PREFIX = "__t18_drill__"
SOURCE_NAME = "__t18_drill_source__"
HF_SOURCE_NAME = "__t18_drill_hf_source__"
OPEN_TOPIC_NAME = "__T18 Open Drill Topic__"
CLOSED_TOPIC_NAME = "__T18 Closed Drill Topic__"
SMOKE_PASSWORD = "t18-smoke-password-not-for-production"

#: A sentinel planted in `audit_logs.metadata_json`. If this string ever appears in a response,
#: the free-form column has been wired to the egress path without anyone deciding what may
#: travel in it.
METADATA_SENTINEL = "__T18_METADATA_SENTINEL__"
IPV6_ADDRESS = "2001:db8::1"
IPV6_ACTION = "__t18_drill_ipv6__"
NO_IPV6_ACTION = "__t18_drill_no_ip__"
METADATA_ACTION = "__t18_drill_metadata__"

ROLES = {"admin": "ADMIN", "viewer": "VIEWER"}

#: Stars are deliberately not in alphabetical order of the repository names: `alpha` / `beta` /
#: `gamma` sorts one way and 100 / 160 / 130 the other, so an assertion about the returned order
#: cannot pass by accident when the `ORDER BY` is dropped.
CATALOG = (
    {"slug": "alpha", "stars": 100, "forks": 4, "language": "Go"},
    {"slug": "beta", "stars": 160, "forks": 9, "language": "Python"},
    {"slug": "gamma", "stars": 130, "forks": 6, "language": "Python"},
)
STARS_DESCENDING = ("__t18_drill__/beta", "__t18_drill__/gamma", "__t18_drill__/alpha")

#: The three snapshots, oldest first. The first day's deltas are `None` because there is no
#: earlier observation to subtract from - the shipped snapshot stage writes `None` there, and a
#: zero would be an assertion that the counters did not move.
SNAPSHOT_SHAPE = ((100, None), (130, 30), (160, 30))

DEFAULT_WINDOW_DAYS = 30
MAX_WINDOW_DAYS = 366


# --------------------------------------------------------------------------------------
# Seeding
# --------------------------------------------------------------------------------------


def purge_drill_rows(session: Session) -> None:
    """Delete every row this drill owns, oldest dependency first.

    Deleting by name rather than by id watermark means the pass is sound even when the database
    was left dirty by an interrupted run, and that running the smoke twice in a row is the same
    as running it once.

    The names are matched as **literal prefixes** (`startswith(..., autoescape=True)`) rather than
    as `LIKE` patterns. A prefix like `__t18_drill__` contains four underscores, and `_` is a
    single-character wildcard in `LIKE`: the pattern `__t18_drill__%` therefore also matches
    `xx/t18-drill-abc`, so a deployment that happened to hold such a repository would have had it
    deleted by this drill. Escaped, the prefix means the prefix.

    The ids are read into Python first instead of being passed as subqueries. MySQL refuses
    `DELETE FROM t WHERE id IN (SELECT ... FROM t)` outright (`1093 You can't specify target
    table for update in FROM clause`), so a correlated delete that looks portable is not: the
    subquery has to be evaluated before the delete is issued, which is what materializing does.
    """
    drill_user_ids = [
        int(value)
        for value in session.scalars(
            select(User.id).where(User.username.startswith(ACTOR_PREFIX, autoescape=True))
        )
    ]
    drill_topic_ids = [
        int(value)
        for value in session.scalars(
            select(Topic.id).where(Topic.name.in_([OPEN_TOPIC_NAME, CLOSED_TOPIC_NAME]))
        )
    ]
    drill_repository_ids = [
        int(value)
        for value in session.scalars(
            select(Repository.id).where(
                Repository.full_name.startswith(NAME_PREFIX, autoescape=True)
            )
        )
    ]

    # Both halves of the audit plan are needed. The actor filter reaches every row an account of
    # the drill caused; the request-id filter is the only thing that reaches the refusals written
    # for a caller that never authenticated, whose `actor_id` is NULL by definition.
    session.execute(delete(AuditLog).where(AuditLog.actor_id.in_(drill_user_ids)))
    session.execute(
        delete(AuditLog).where(AuditLog.request_id.startswith(REQUEST_ID_PREFIX, autoescape=True))
    )
    session.execute(
        delete(RepoSnapshot).where(RepoSnapshot.repository_id.in_(drill_repository_ids))
    )
    session.execute(delete(TopicRepository).where(TopicRepository.topic_id.in_(drill_topic_ids)))
    session.execute(
        delete(TopicRepository).where(TopicRepository.repository_id.in_(drill_repository_ids))
    )
    session.execute(delete(Repository).where(Repository.id.in_(drill_repository_ids)))
    session.execute(delete(Topic).where(Topic.id.in_(drill_topic_ids)))
    session.execute(delete(DataSource).where(DataSource.name.in_([SOURCE_NAME, HF_SOURCE_NAME])))
    session.execute(delete(UserRole).where(UserRole.user_id.in_(drill_user_ids)))
    session.execute(delete(User).where(User.id.in_(drill_user_ids)))
    session.commit()


def count_drill_rows(session: Session) -> dict[str, int]:
    """Count the rows the drill still owns, by the same literal prefixes the purge uses.

    This is how the promise "leave the database as it was found" becomes a measurement instead of
    a comment: the smoke reports what it left behind, and the verification script asserts the
    count is zero. Counting by prefix rather than by "rows newer than X" keeps the measurement
    honest on a database another process is also writing to.
    """
    drill_user_ids = [
        int(value)
        for value in session.scalars(
            select(User.id).where(User.username.startswith(ACTOR_PREFIX, autoescape=True))
        )
    ]
    return {
        "users": len(drill_user_ids),
        "repositories": int(
            session.scalar(
                select(func.count())
                .select_from(Repository)
                .where(Repository.full_name.startswith(NAME_PREFIX, autoescape=True))
            )
            or 0
        ),
        "topics": int(
            session.scalar(
                select(func.count())
                .select_from(Topic)
                .where(Topic.name.in_([OPEN_TOPIC_NAME, CLOSED_TOPIC_NAME]))
            )
            or 0
        ),
        # Row count, not a flag: the residue this guards against is exactly the row that no
        # actor-based purge could see, so it has to be counted by its own attribute.
        "audit_rows": int(
            session.scalar(
                select(func.count())
                .select_from(AuditLog)
                .where(AuditLog.request_id.startswith(REQUEST_ID_PREFIX, autoescape=True))
            )
            or 0
        ),
    }


def seed_actors(session: Session) -> dict[str, int]:
    """Create the drill's two accounts and return their ids by role name."""
    for code, name in (("ADMIN", "Administrator"), ("VIEWER", "Viewer")):
        if session.scalar(select(Role.id).where(Role.code == code)) is None:
            session.add(Role(code=code, name=name))
    session.flush()

    ids: dict[str, int] = {}
    for suffix, role in ROLES.items():
        user = create_user(
            session,
            username=f"{ACTOR_PREFIX}{suffix}__",
            password_hash=hash_password(SMOKE_PASSWORD),
            display_name=f"T18 {suffix}",
            role_codes={role},
            data_scope="personal",
        )
        ids[suffix] = int(user.id)
    session.commit()
    return ids


def _ensure_source(session: Session, *, name: str, source_type: str, base_url: str) -> DataSource:
    """Return the source for one provider, creating it only if the deployment has none.

    `data_sources.source_type` is unique, so a deployment has at most one GitHub source and the
    drill has to borrow it rather than add a second. Borrowing is also the more honest thing to
    do: the `provider` column the catalog exposes comes from this row, so reusing the row the
    application itself created is what makes the assertion about real configuration.
    """
    existing = session.scalar(
        select(DataSource).where(
            or_(DataSource.name == name, DataSource.source_type == source_type)
        )
    )
    if existing is not None:
        return existing
    source = DataSource(
        name=name,
        source_type=source_type,
        base_url=base_url,
        enabled=True,
        rate_limit_per_minute=30,
    )
    session.add(source)
    session.flush()
    return source


def seed_catalog(session: Session, *, actor_id: int) -> dict[str, Any]:
    """Create two sources, three repositories, two topics, and three days of snapshots.

    Two sources because the `provider` column has to come from the joined source row rather than
    from a constant, and one repository sits on the second source so the join is exercised.
    """
    github = _ensure_source(
        session,
        name=SOURCE_NAME,
        source_type="github",
        base_url="https://api.github.com",
    )
    huggingface = _ensure_source(
        session,
        name=HF_SOURCE_NAME,
        source_type="huggingface",
        base_url="https://hf-mirror.com/api",
    )

    now = utc_now()
    repository_ids: dict[str, int] = {}
    for index, spec in enumerate(CATALOG):
        # One row on the second source, one row with no language, one row with a description that
        # only the `q` filter can reach - each one exists so a filter assertion has a row that
        # must be excluded as well as a row that must be found.
        source = huggingface if spec["slug"] == "gamma" else github
        repository = Repository(
            source_id=source.id,
            provider_repo_id=f"{NAME_PREFIX}{spec['slug']}",
            full_name=f"{NAME_PREFIX}/{spec['slug']}",
            html_url=f"https://example.com/{NAME_PREFIX}/{spec['slug']}",
            description=(
                "T18 measurable drift sentinel" if spec["slug"] == "alpha" else "drill repository"
            ),
            language=None if spec["slug"] == "alpha" else str(spec["language"]),
            license="MIT",
            topics_json=["drill"],
            stars=int(spec["stars"]),
            forks=int(spec["forks"]),
            first_seen_at=now - timedelta(days=10 + index),
            last_seen_at=now,
        )
        session.add(repository)
        session.flush()
        repository_ids[spec["slug"]] = int(repository.id)

        today = now.date()
        for offset, (stars, delta) in enumerate(SNAPSHOT_SHAPE):
            session.add(
                RepoSnapshot(
                    repository_id=repository.id,
                    snapshot_date=today - timedelta(days=len(SNAPSHOT_SHAPE) - 1 - offset),
                    stars=stars + index,
                    forks=int(spec["forks"]),
                    open_issues=1,
                    stars_delta=delta,
                    forks_delta=delta,
                )
            )

    open_topic = Topic(
        name=OPEN_TOPIC_NAME,
        description="T18 smoke - visible to every role",
        keywords_json=[NAME_PREFIX],
        min_stars=0,
        window_days=7,
        max_candidates=3,
        publish_mode="MANUAL",
        enabled=True,
        created_by=actor_id,
    )
    closed_topic = Topic(
        name=CLOSED_TOPIC_NAME,
        description="T18 smoke - unpublished configuration",
        keywords_json=[NAME_PREFIX],
        min_stars=0,
        window_days=7,
        max_candidates=3,
        publish_mode="MANUAL",
        enabled=False,
        created_by=actor_id,
    )
    session.add_all([open_topic, closed_topic])
    session.flush()

    # Rank 1 goes to the *lowest* star count, so a slice ordered by rank cannot be mistaken for a
    # slice ordered by stars.
    for slug, (score, rank) in (
        ("alpha", (Decimal("0.900000"), 1)),
        ("beta", (Decimal("0.500000"), 2)),
        ("gamma", (Decimal("0.100000"), 3)),
    ):
        for topic in (open_topic, closed_topic):
            session.add(
                TopicRepository(
                    topic_id=topic.id,
                    repository_id=repository_ids[slug],
                    score=score,
                    rank=rank,
                    selected_for_agent=rank <= 2,
                )
            )

    session.commit()
    return {
        "open_topic_id": int(open_topic.id),
        "closed_topic_id": int(closed_topic.id),
        "repository_ids": repository_ids,
    }


def seed_audit_rows(session: Session, *, actor_id: int) -> None:
    """Stage the three audit rows the endpoint's own fields are read from."""
    record_audit_event(
        session,
        action=IPV6_ACTION,
        resource_type="drill",
        result="success",
        actor_id=actor_id,
        ip_address=IPV6_ADDRESS,
    )
    record_audit_event(
        session,
        action=NO_IPV6_ACTION,
        resource_type="drill",
        result="success",
        actor_id=actor_id,
    )
    record_audit_event(
        session,
        action=METADATA_ACTION,
        resource_type="drill",
        result="success",
        actor_id=actor_id,
        metadata_json={"note": METADATA_SENTINEL},
    )
    session.commit()


# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------


def _api_login(client: TestClient, suffix: str) -> dict[str, str]:
    """Return the bearer header for one drill account."""
    response = client.post(
        "/api/v1/auth/login",
        json={"username": f"{ACTOR_PREFIX}{suffix}__", "password": SMOKE_PASSWORD},
    )
    if response.status_code != 200:
        raise RuntimeError(f"API login for {suffix} failed: {response.status_code} {response.text}")
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _get(
    client: TestClient,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
) -> Any:
    """Issue one catalog or audit request and return the response."""
    return client.get(path, headers=headers, params=params)


def _body(response: Any) -> dict[str, Any]:
    """Return a successful response's JSON body, or raise with the status and body if not."""
    if response.status_code != 200:
        raise RuntimeError(f"expected 200, got {response.status_code}: {response.text}")
    payload: dict[str, Any] = response.json()
    return payload


def _catalog(client: TestClient, headers: dict[str, str], **params: Any) -> dict[str, Any]:
    """Read the drill's slice of the catalog."""
    return _body(_get(client, "/api/v1/repositories", headers=headers, params=params))


# --------------------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------------------


def catalog_checks(
    client: TestClient,
    viewer: dict[str, str],
    admin: dict[str, str],
    seeded: dict[str, Any],
) -> dict[str, Any]:
    """Drive the repository catalog endpoints and report what they answered."""
    drill = {"q": NAME_PREFIX}

    anonymous = _get(client, "/api/v1/repositories", params=drill)
    listed = _catalog(client, viewer, **drill)
    page_one = _catalog(client, viewer, page=1, page_size=2, **drill)
    page_two = _catalog(client, viewer, page=2, page_size=2, **drill)
    ranked = _catalog(client, viewer, topic_id=seeded["open_topic_id"], **drill)

    closed = _get(
        client,
        "/api/v1/repositories",
        headers=viewer,
        params={"topic_id": seeded["closed_topic_id"], **drill},
    )
    closed_admin = _get(
        client,
        "/api/v1/repositories",
        headers=admin,
        params={"topic_id": seeded["closed_topic_id"], **drill},
    )

    alpha = next(item for item in listed["items"] if item["full_name"].endswith("/alpha"))
    gamma = next(item for item in listed["items"] if item["full_name"].endswith("/gamma"))

    return {
        "anonymous_status": anonymous.status_code,
        "total": listed["total"],
        "full_names_ordered": [item["full_name"] for item in listed["items"]],
        "providers": sorted({item["provider"] for item in listed["items"]}),
        "gamma_provider": gamma["provider"],
        "page_one_ids": [item["id"] for item in page_one["items"]],
        "page_two_ids": [item["id"] for item in page_two["items"]],
        "paging_overlap": len(
            {item["id"] for item in page_one["items"]} & {item["id"] for item in page_two["items"]}
        ),
        # No `topic_id` means no ranking was ever computed for this row, which is not the same
        # answer as "the score is zero" or "it was not selected".
        "unranked_fields": [
            [item["score"], item["rank"], item["selected_for_agent"]] for item in listed["items"]
        ],
        "language_filter_total": _catalog(client, viewer, language="PYTHON", **drill)["total"],
        "min_stars_total": _catalog(client, viewer, min_stars=140, **drill)["total"],
        "description_query_total": _catalog(client, viewer, q="measurable drift")["total"],
        "future_created_after_total": _catalog(
            client,
            viewer,
            created_after="2099-01-01T00:00:00Z",
            **drill,
        )["total"],
        "unknown_sort_status": _get(
            client, "/api/v1/repositories", headers=viewer, params={"sort": "name", **drill}
        ).status_code,
        "page_size_cap_status": _get(
            client, "/api/v1/repositories", headers=viewer, params={"page_size": 101, **drill}
        ).status_code,
        # The T12-through-T18 boundary defect: the query builder refuses a naive timestamp, and
        # the caller has to be told that at the edge rather than by a 500.
        "naive_timestamp_status": _get(
            client,
            "/api/v1/repositories",
            headers=viewer,
            params={"created_after": "2026-09-24T00:00:00", **drill},
        ).status_code,
        "naive_timestamp_detail": _get(
            client,
            "/api/v1/repositories",
            headers=viewer,
            params={"created_after": "2026-09-24T00:00:00", **drill},
        ).json()["detail"],
        "ranked_ranks": sorted(item["rank"] for item in ranked["items"]),
        "ranked_selected": [item["selected_for_agent"] for item in ranked["items"]],
        "ranked_score_of_alpha": next(
            item["score"] for item in ranked["items"] if item["full_name"].endswith("/alpha")
        ),
        "closed_topic_status": closed.status_code,
        "closed_topic_leaks_name": CLOSED_TOPIC_NAME in closed.text,
        "closed_topic_admin_status": closed_admin.status_code,
        "closed_topic_admin_total": _body(closed_admin)["total"],
        "missing_topic_status": _get(
            client, "/api/v1/repositories", headers=viewer, params={"topic_id": 999_999_999}
        ).status_code,
        "alpha_language": alpha["language"],
        "alpha_topics": alpha["topics"],
    }


def snapshot_checks(
    client: TestClient,
    viewer: dict[str, str],
    seeded: dict[str, Any],
) -> dict[str, Any]:
    """Drive the repository trend endpoint and report what it answered."""
    repository_id = seeded["repository_ids"]["alpha"]
    path = f"/api/v1/repositories/{repository_id}/snapshots"
    today: date = utc_now().date()
    first = today - timedelta(days=len(SNAPSHOT_SHAPE) - 1)

    explicit = _body(
        _get(
            client,
            path,
            headers=viewer,
            params={"start": first.isoformat(), "end": today.isoformat()},
        )
    )
    defaulted = _body(_get(client, path, headers=viewer))
    # The status is taken off the response before `_body` asserts it, so the payload reports what
    # the endpoint answered rather than restating the 200 `_body` demands. A field that is written
    # as a constant cannot support an assertion: "an empty window is a 200, not a 404" is only a
    # claim about the endpoint if the 200 was observed here.
    empty_response = _get(
        client,
        path,
        headers=viewer,
        params={
            "start": (today - timedelta(days=5)).isoformat(),
            "end": (today - timedelta(days=4)).isoformat(),
        },
    )
    empty_status = empty_response.status_code
    empty = _body(empty_response)
    single = _body(
        _get(
            client,
            path,
            headers=viewer,
            params={"start": today.isoformat(), "end": today.isoformat()},
        )
    )

    return {
        "closed_window_dates": [item["snapshot_date"] for item in explicit["items"]],
        "closed_window_resolved": [explicit["start_date"], explicit["end_date"]],
        # The window this drill *asked* for, reported alongside the one it got. The two are equal
        # precisely because the range is closed at both ends, and that comparison is what the
        # verification script can assert without hard-coding a date: naming the days here would
        # make the assertion true only on the calendar day the drill was written.
        "requested_window": [first.isoformat(), today.isoformat()],
        # The first observation has nothing to subtract from, so the delta is `None` rather than
        # `0`. The distinction is the same one the rank fields make.
        "first_day_delta": explicit["items"][0]["stars_delta"],
        "second_day_delta": explicit["items"][1]["stars_delta"],
        "default_window_count": len(defaulted["items"]),
        "default_window_days": (
            date.fromisoformat(defaulted["end_date"]) - date.fromisoformat(defaulted["start_date"])
        ).days,
        "empty_window_status": empty_status,
        "empty_window_count": len(empty["items"]),
        "single_day_count": len(single["items"]),
        "single_day_date": single["items"][0]["snapshot_date"] if single["items"] else None,
        "inverted_window_status": _get(
            client,
            path,
            headers=viewer,
            params={"start": today.isoformat(), "end": first.isoformat()},
        ).status_code,
        "overlong_window_status": _get(
            client,
            path,
            headers=viewer,
            params={
                "start": (today - timedelta(days=MAX_WINDOW_DAYS + 10)).isoformat(),
                "end": today.isoformat(),
            },
        ).status_code,
        "unknown_granularity_status": _get(
            client, path, headers=viewer, params={"granularity": "week"}
        ).status_code,
        "missing_repository_status": _get(
            client, "/api/v1/repositories/999999999/snapshots", headers=viewer
        ).status_code,
        "anonymous_status": _get(client, path).status_code,
    }


def audit_checks(
    client: TestClient,
    viewer: dict[str, str],
    admin: dict[str, str],
    actor_ids: dict[str, int],
    factory: sessionmaker[Session],
) -> dict[str, Any]:
    """Drive the audit endpoint and report what it answered."""
    refused = _get(client, "/api/v1/audit-logs", headers=viewer)
    # Observed, not restated: see the note on `empty_window_status`.
    trail_response = _get(client, "/api/v1/audit-logs", headers=admin, params={"page_size": 100})
    admin_status = trail_response.status_code
    trail = _body(trail_response)
    returned_ids = [int(item["id"]) for item in trail["items"]]
    admin_trail = _body(
        _get(
            client,
            "/api/v1/audit-logs",
            headers=admin,
            params={"actor_id": actor_ids["admin"], "page_size": 100},
        )
    )

    ipv6 = _body(_get(client, "/api/v1/audit-logs", headers=admin, params={"action": IPV6_ACTION}))
    no_ipv6 = _body(
        _get(client, "/api/v1/audit-logs", headers=admin, params={"action": NO_IPV6_ACTION})
    )
    metadata_response = _get(
        client, "/api/v1/audit-logs", headers=admin, params={"action": METADATA_ACTION}
    )

    with factory() as session:
        refusal = session.scalars(
            select(AuditLog).where(
                AuditLog.actor_id == actor_ids["viewer"],
                AuditLog.action == "auth.rbac",
                AuditLog.result == "denied",
            )
        ).all()
        metadata_stored = session.scalar(
            select(AuditLog.metadata_json).where(AuditLog.action == METADATA_ACTION)
        )

    return {
        "anonymous_status": _get(client, "/api/v1/audit-logs").status_code,
        "viewer_status": refused.status_code,
        "viewer_refusals": len(refusal),
        "viewer_refusal_actor": refusal[0].actor_id if refusal else None,
        "viewer_refusal_resource_type": refusal[0].resource_type if refusal else None,
        "admin_status": admin_status,
        "newest_first": returned_ids == sorted(returned_ids, reverse=True),
        # The newest row is the *viewer's* refusal, because the refusal was the last thing that
        # happened before the administrator read the trail. `admin_total` is the stronger claim:
        # the administrator's own three seeded rows plus its login, which means the endpoint is
        # reading the ledger the application writes rather than a view built for this drill.
        "newest_action": trail["items"][0]["action"],
        "newest_actor": trail["items"][0]["actor_id"],
        "admin_total": admin_trail["total"],
        "admin_newest_action": admin_trail["items"][0]["action"],
        "ipv6_rendered": ipv6["items"][0]["ip_address"] if ipv6["items"] else None,
        "missing_ip_rendered": no_ipv6["items"][0]["ip_address"] if no_ipv6["items"] else "absent",
        # `metadata_json` is stored - that is what makes this assertion mean something - and it
        # must not be readable through the API.
        "metadata_stored": metadata_stored is not None,
        "metadata_leaked": METADATA_SENTINEL in metadata_response.text
        or "metadata_json" in metadata_response.text,
        "unknown_result_status": _get(
            client, "/api/v1/audit-logs", headers=admin, params={"result": "maybe"}
        ).status_code,
        "page_size_cap_status": _get(
            client, "/api/v1/audit-logs", headers=admin, params={"page_size": 101}
        ).status_code,
        "naive_timestamp_status": _get(
            client,
            "/api/v1/audit-logs",
            headers=admin,
            params={"created_before": "2026-09-24T00:00:00"},
        ).status_code,
    }


def topic_denial_audit(factory: sessionmaker[Session], viewer_id: int) -> dict[str, Any]:
    """Report the audit row the refused topic slice left behind.

    Read from the database rather than through the API because the caller being checked is the
    one who was refused: a refusal the refused party can read about is not the same control.
    """
    with factory() as session:
        rows = session.scalars(
            select(AuditLog).where(
                AuditLog.actor_id == viewer_id,
                AuditLog.action == "repository.read",
                AuditLog.resource_type == "topic",
            )
        ).all()
    return {
        "count": len(rows),
        "resource_ids": sorted(row.resource_id or "" for row in rows),
        "results": sorted({row.result for row in rows}),
    }


def documentation_checks() -> dict[str, Any]:
    """Run the ten documentation comparisons in-process.

    Imported rather than re-implemented: the point of the checker is that it is the same
    comparison every time, and a second copy here would be a second thing to keep in step.
    """
    import check_docs_drift

    results = [check() for check in check_docs_drift.CHECKS]
    return {
        "passed": all(result.ok for result in results),
        # A joined string rather than a list, because the consumer is a PowerShell assertion and
        # `@($null).Count` is 1: an empty JSON array and an absent one are indistinguishable once
        # they have been through `ConvertFrom-Json`, so "no drift" has to be expressible as a
        # scalar. `failures` stays for whoever reads the payload.
        "drifted": ", ".join(sorted(result.name for result in results if result.failures)),
        "failures": {result.name: result.failures for result in results if result.failures},
        "run": len(results),
    }


# --------------------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    """Parse the optional cleanup mode used by the verification script."""
    parser = argparse.ArgumentParser(description="Run the T18 documentation-sync smoke")
    parser.add_argument(
        "--cleanup-only",
        action="store_true",
        help="Only remove the drill's rows and exit",
    )
    return parser.parse_args()


def main() -> None:
    """Run the smoke against the configured database and print the assertion payload."""
    args = _parse_args()
    settings: Settings = get_settings()
    engine = create_database_engine(settings.database_url)
    factory = create_session_factory(engine)

    if args.cleanup_only:
        with factory() as session:
            purge_drill_rows(session)
        engine.dispose()
        print(json.dumps({"cleanup_only": True}, ensure_ascii=False, sort_keys=True))
        return

    payload: dict[str, Any] = {}
    try:
        with factory() as session:
            # Clean before seeding, so an interrupted previous run cannot inflate a `total` and
            # make the drill pass for the wrong reason.
            purge_drill_rows(session)
            actor_ids = seed_actors(session)
            seeded = seed_catalog(session, actor_id=actor_ids["admin"])
            seed_audit_rows(session, actor_id=actor_ids["admin"])

        with TestClient(create_app(settings)) as client:
            # Every request carries a drill-owned correlation id. The middleware copies it into
            # `request.state.request_id`, the audit writer stores it, and the purge deletes by it -
            # which is the only handle on the rows a *refusal to an unauthenticated caller* writes,
            # since those carry no actor. HTTPX merges these client-level headers into each
            # request, so the per-call `Authorization` headers still apply.
            client.headers["X-Request-ID"] = f"{REQUEST_ID_PREFIX}{uuid4().hex[:8]}"
            admin = _api_login(client, "admin")
            viewer = _api_login(client, "viewer")
            payload["catalog"] = catalog_checks(client, viewer, admin, seeded)
            payload["snapshots"] = snapshot_checks(client, viewer, seeded)
            payload["audit"] = audit_checks(client, viewer, admin, actor_ids, factory)
            payload["catalog"]["closed_topic_audit"] = topic_denial_audit(
                factory, actor_ids["viewer"]
            )
        payload["documentation"] = documentation_checks()
        payload["expected"] = {
            "max_window_days": MAX_WINDOW_DAYS,
            "default_window_days": DEFAULT_WINDOW_DAYS,
            "name_prefix": NAME_PREFIX,
            "open_topic_id": seeded["open_topic_id"],
            "closed_topic_id": seeded["closed_topic_id"],
            "admin_id": actor_ids["admin"],
            "viewer_id": actor_ids["viewer"],
        }
    finally:
        with factory() as session:
            purge_drill_rows(session)
            # Measured after the purge, so the payload answers "what did this run leave behind?"
            # rather than "what did it create?". All four counts are zero on a clean pass; the
            # verification script asserts that, which is what turns the promise printed as
            # "Leave the database as it was found" into something that can fail.
            payload["leftovers"] = count_drill_rows(session)
        engine.dispose()

    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
