"""Integration tests for the repository catalog and trend endpoints.

`REQUIREMENTS.md` FR-D-05 requires "报告、**仓库**、Agent、评测和审计 API"; T18 added the two
repository routes and the audit route, so these tests pin what the requirement means in practice:
the catalog is the shared public metadata, the trend window includes both days the caller named,
and the one filter that carries an authorization decision (`topic_id`) actually carries it.

Seeding goes through one session and commits, because the shared in-memory database lends a
single connection to every session - an open seeding transaction would block the application's.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from api_support import Actor, auth_headers, build_client, create_actor
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from support import Workspace

from open_source_intelligence.persistence.models.audit import AuditLog
from open_source_intelligence.pipeline.queries import MAX_PAGE_SIZE

CATALOG = "/api/v1/repositories"


@dataclass(frozen=True, slots=True)
class Catalog:
    """The seeded catalog plus the ids the assertions need."""

    open_topic_id: int
    closed_topic_id: int
    ranked_repository_id: int
    unranked_repository_id: int


@pytest.fixture()
def client(session_factory: sessionmaker[Session]) -> Iterator[TestClient]:
    """Provide a client bound to the shared test database."""
    with build_client(session_factory) as started:
        yield started


@pytest.fixture()
def admin(session: Session) -> Actor:
    """An administrator, who reads every topic including a disabled one."""
    return create_actor(session, username="admin", role="ADMIN")


@pytest.fixture()
def viewer(session: Session) -> Actor:
    """A read-only user: the catalog is public, the disabled topic is not."""
    return create_actor(session, username="viewer", role="VIEWER")


@pytest.fixture()
def catalog(session: Session, workspace: Workspace) -> Catalog:
    """Seed two sources, three repositories, two topics and a short snapshot series."""
    github = workspace.add_source(source_type="github")
    huggingface = workspace.add_source(source_type="huggingface")

    # Stars are deliberately out of alphabetical order so a test that asserted "the order looks
    # right" would still fail if the sort were dropped.
    ranked = workspace.add_repository(
        source_id=github.id,
        provider_repo_id="r-1",
        full_name="acme/rocket",
        stars=900,
        forks=90,
        language="Python",
        description="A tool-using agent runtime",
    )
    middle = workspace.add_repository(
        source_id=github.id,
        provider_repo_id="r-2",
        full_name="globex/compiler",
        stars=500,
        forks=500,
        language="Rust",
        description="A fast compiler",
    )
    smallest = workspace.add_repository(
        source_id=huggingface.id,
        provider_repo_id="r-3",
        full_name="initech/dataset",
        stars=100,
        forks=10,
        language="Jupyter Notebook",
        description=None,
    )

    open_topic = workspace.add_topic(name="Open topic", enabled=True)
    closed_topic = workspace.add_topic(name="Closed topic", enabled=False)
    workspace.link(open_topic.id, ranked.id, rank=1, score=Decimal("12.5"), selected=True)
    workspace.link(open_topic.id, middle.id, rank=2, score=Decimal("4.25"), selected=False)
    workspace.link(closed_topic.id, smallest.id, rank=1, score=Decimal("1"), selected=True)

    first_day = date(2026, 9, 20)
    for offset, stars in enumerate((880, 900, 900)):
        workspace.add_snapshot(
            ranked.id,
            first_day + timedelta(days=offset),
            stars=stars,
            forks=90,
            open_issues=3,
            stars_delta=None if offset == 0 else 20,
            forks_delta=None if offset == 0 else 0,
        )
    session.commit()
    return Catalog(
        open_topic_id=open_topic.id,
        closed_topic_id=closed_topic.id,
        ranked_repository_id=ranked.id,
        unranked_repository_id=smallest.id,
    )


def test_catalog_requires_a_session(client: TestClient) -> None:
    """The catalog is authenticated even though everything in it is public metadata."""
    assert client.get(CATALOG).status_code == 401


def test_catalog_returns_the_provider_and_public_metadata(
    client: TestClient,
    viewer: Actor,
    catalog: Catalog,
) -> None:
    """`provider` comes from the source row, not from the repository."""
    response = client.get(CATALOG, headers=auth_headers(client, viewer))
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 3
    by_name = {item["full_name"]: item for item in body["items"]}
    assert by_name["acme/rocket"]["provider"] == "github"
    assert by_name["initech/dataset"]["provider"] == "huggingface"
    assert by_name["acme/rocket"]["license"] == "MIT"
    assert by_name["acme/rocket"]["topics"] == ["ai", "llm"]


def test_ranking_is_absent_unless_the_list_is_sliced_by_topic(
    client: TestClient,
    viewer: Actor,
    catalog: Catalog,
) -> None:
    """Score, rank and the selection flag are a topic's output, so without a topic they are null.

    Returning `0` or `false` instead would be a claim about a ranking pass that never happened.
    """
    response = client.get(CATALOG, headers=auth_headers(client, viewer))
    assert response.status_code == 200
    for item in response.json()["items"]:
        assert item["score"] is None
        assert item["rank"] is None
        assert item["selected_for_agent"] is None


def test_catalog_is_sorted_by_stars_and_pages_stably(
    client: TestClient,
    viewer: Actor,
    catalog: Catalog,
) -> None:
    """Sorting is descending by stars, with the id tiebreaker keeping pages disjoint."""
    headers = auth_headers(client, viewer)
    first = client.get(CATALOG, headers=headers, params={"page_size": 2}).json()
    second = client.get(CATALOG, headers=headers, params={"page_size": 2, "page": 2}).json()
    assert [item["full_name"] for item in first["items"]] == ["acme/rocket", "globex/compiler"]
    assert [item["full_name"] for item in second["items"]] == ["initech/dataset"]
    assert first["total"] == second["total"] == 3


def test_unknown_sort_is_refused_rather_than_defaulted(
    client: TestClient,
    viewer: Actor,
    catalog: Catalog,
) -> None:
    """A caller that asked for an ordering they did not get has no way to notice."""
    response = client.get(
        CATALOG,
        headers=auth_headers(client, viewer),
        params={"sort": "stars_delta"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"


def test_page_size_is_bounded(
    client: TestClient,
    viewer: Actor,
    catalog: Catalog,
) -> None:
    """The platform-wide 1-100 page bound applies here too."""
    over = client.get(
        CATALOG,
        headers=auth_headers(client, viewer),
        params={"page_size": MAX_PAGE_SIZE + 1},
    )
    assert over.status_code == 422


def test_catalog_filters_narrow_the_total_as_well_as_the_page(
    client: TestClient,
    viewer: Actor,
    catalog: Catalog,
) -> None:
    """`total` has to obey the same filters, or a caller pages into emptiness."""
    headers = auth_headers(client, viewer)
    by_language = client.get(CATALOG, headers=headers, params={"language": "rust"}).json()
    assert by_language["total"] == 1
    assert by_language["items"][0]["full_name"] == "globex/compiler"

    by_stars = client.get(CATALOG, headers=headers, params={"min_stars": 400}).json()
    assert {item["full_name"] for item in by_stars["items"]} == {
        "acme/rocket",
        "globex/compiler",
    }
    assert by_stars["total"] == 2

    by_query = client.get(CATALOG, headers=headers, params={"q": "COMPILER"}).json()
    assert [item["full_name"] for item in by_query["items"]] == ["globex/compiler"]

    # `description` is nullable, so a substring search must not drop the null-description row
    # from every query - only from the ones that do not match it.
    null_description = client.get(CATALOG, headers=headers, params={"q": "dataset"}).json()
    assert [item["full_name"] for item in null_description["items"]] == ["initech/dataset"]

    future = (datetime(2026, 10, 12, 12, 0, tzinfo=UTC) + timedelta(days=1)).isoformat()
    after = client.get(CATALOG, headers=headers, params={"created_after": future}).json()
    assert after["total"] == 0


def test_a_text_filter_is_a_literal_substring_rather_than_a_like_pattern(
    client: TestClient,
    session: Session,
    viewer: Actor,
    catalog: Catalog,
    workspace: Workspace,
) -> None:
    """`q` is a substring search, so `%` and `_` are characters and not `LIKE` wildcards.

    Passed straight into `LIKE`, `q=_` becomes `LIKE '%_%'` and matches every row that has at
    least one character, while `q=%` matches the entire catalog - so a caller searching for the
    literal character is answered with the opposite of what they asked for. The third case is the
    other half: a name that really contains the character still has to be found, otherwise
    "escape everything so nothing ever matches" would satisfy this test as well.
    """
    gitlab = workspace.add_source(source_type="gitlab")
    workspace.add_repository(
        source_id=gitlab.id,
        provider_repo_id="r-4",
        full_name="octo/under_score",
        stars=10,
        forks=1,
        language="Go",
        description="a name that really contains an underscore",
    )
    session.commit()

    headers = auth_headers(client, viewer)
    literal = client.get(CATALOG, headers=headers, params={"q": "_"}).json()
    assert [item["full_name"] for item in literal["items"]] == ["octo/under_score"]
    assert literal["total"] == 1

    wildcard = client.get(CATALOG, headers=headers, params={"q": "%"}).json()
    assert wildcard["total"] == 0
    assert wildcard["items"] == []


def test_a_timestamp_without_an_offset_is_refused_at_the_boundary(
    client: TestClient,
    viewer: Actor,
    catalog: Catalog,
) -> None:
    """A naive timestamp is a `422` that names the fix, not a `500` from inside the query.

    `UTCDateTime` refuses to guess the zone, which is right; without the shared parameter type
    that refusal reaches the caller as an internal error and reads like an outage.
    """
    response = client.get(
        CATALOG,
        headers=auth_headers(client, viewer),
        params={"created_after": "2026-10-13T12:00:00"},
    )
    assert response.status_code == 422
    assert response.json()["code"] == "VALIDATION_ERROR"
    assert "timezone offset" in response.text


def test_topic_slice_carries_the_ranking(
    client: TestClient,
    viewer: Actor,
    catalog: Catalog,
) -> None:
    """Slicing by a topic returns that topic's candidates with their ranking attached."""
    response = client.get(
        CATALOG,
        headers=auth_headers(client, viewer),
        params={"topic_id": catalog.open_topic_id},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    ranked = next(item for item in body["items"] if item["full_name"] == "acme/rocket")
    assert ranked["rank"] == 1
    assert ranked["score"] == "12.500000"
    assert ranked["selected_for_agent"] is True
    unselected = next(item for item in body["items"] if item["full_name"] == "globex/compiler")
    assert unselected["selected_for_agent"] is False


def test_a_disabled_topic_is_refused_and_audited(
    client: TestClient,
    viewer: Actor,
    catalog: Catalog,
    session: Session,
) -> None:
    """Membership is the part of the catalog that needs adjudicating, and a refusal is recorded.

    The `403` body says nothing about the topic; the audit row names it. Same mirror as the
    report ACL: the caller learns nothing, the operator can still investigate.
    """
    response = client.get(
        CATALOG,
        headers=auth_headers(client, viewer),
        params={"topic_id": catalog.closed_topic_id},
    )
    assert response.status_code == 403
    assert "Closed topic" not in response.text
    assert "topic" in response.json()["detail"]

    denied = session.scalars(
        select(AuditLog).where(
            AuditLog.action == "repository.read",
            AuditLog.result == "denied",
        )
    ).all()
    assert [row.resource_id for row in denied] == [str(catalog.closed_topic_id)]
    assert [row.actor_id for row in denied] == [viewer.user_id]
    assert denied[0].resource_type == "topic"


def test_an_administrator_may_slice_by_a_disabled_topic(
    client: TestClient,
    admin: Actor,
    catalog: Catalog,
) -> None:
    """`ADMIN` reads every topic; that is the whole of the rule."""
    response = client.get(
        CATALOG,
        headers=auth_headers(client, admin),
        params={"topic_id": catalog.closed_topic_id},
    )
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 1


def test_missing_topic_is_a_not_found(
    client: TestClient,
    viewer: Actor,
    catalog: Catalog,
) -> None:
    """A topic that does not exist is `404`, distinct from the `403` of an invisible one."""
    response = client.get(
        CATALOG,
        headers=auth_headers(client, viewer),
        params={"topic_id": catalog.closed_topic_id + 999},
    )
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"


def test_snapshot_window_includes_both_named_days(
    client: TestClient,
    viewer: Actor,
    catalog: Catalog,
) -> None:
    """The closed window is the one thing this route does differently, so it is pinned here."""
    headers = auth_headers(client, viewer)
    response = client.get(
        f"{CATALOG}/{catalog.ranked_repository_id}/snapshots",
        headers=headers,
        params={"start": "2026-09-20", "end": "2026-09-22"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["provider"] == "github"
    assert body["granularity"] == "day"
    assert [item["snapshot_date"] for item in body["items"]] == [
        "2026-09-20",
        "2026-09-21",
        "2026-09-22",
    ]
    # The first day has no predecessor, so its delta is absent rather than zero.
    assert body["items"][0]["stars_delta"] is None
    assert body["items"][1]["stars_delta"] == 20


def test_snapshot_window_edge_days_are_included(
    client: TestClient,
    viewer: Actor,
    catalog: Catalog,
) -> None:
    """Naming a single day returns that day; a half-open range would return nothing."""
    response = client.get(
        f"{CATALOG}/{catalog.ranked_repository_id}/snapshots",
        headers=auth_headers(client, viewer),
        params={"start": "2026-09-22", "end": "2026-09-22"},
    )
    assert response.status_code == 200
    assert [item["snapshot_date"] for item in response.json()["items"]] == ["2026-09-22"]


def test_empty_snapshot_window_is_an_answer_not_an_error(
    client: TestClient,
    viewer: Actor,
    catalog: Catalog,
) -> None:
    """A repository with no snapshots in range is still a repository."""
    response = client.get(
        f"{CATALOG}/{catalog.ranked_repository_id}/snapshots",
        headers=auth_headers(client, viewer),
        params={"start": "2026-01-01", "end": "2026-01-05"},
    )
    assert response.status_code == 200
    assert response.json()["items"] == []


def test_snapshot_window_bounds_are_refused_not_clamped(
    client: TestClient,
    viewer: Actor,
    catalog: Catalog,
) -> None:
    """A clamped window silently answers about a different period."""
    headers = auth_headers(client, viewer)
    path = f"{CATALOG}/{catalog.ranked_repository_id}/snapshots"
    inverted = client.get(
        path,
        headers=headers,
        params={"start": "2026-09-22", "end": "2026-09-20"},
    )
    assert inverted.status_code == 422

    too_wide = client.get(
        path,
        headers=headers,
        params={"start": "2020-01-01", "end": "2026-09-20"},
    )
    assert too_wide.status_code == 422


def test_unknown_granularity_is_refused(
    client: TestClient,
    viewer: Actor,
    catalog: Catalog,
) -> None:
    """Only `day` is implemented, so only `day` is accepted."""
    response = client.get(
        f"{CATALOG}/{catalog.ranked_repository_id}/snapshots",
        headers=auth_headers(client, viewer),
        params={"granularity": "hour"},
    )
    assert response.status_code == 422


def test_unknown_repository_is_a_not_found(
    client: TestClient,
    viewer: Actor,
    catalog: Catalog,
) -> None:
    """The repository is the resource here, so its absence is the `404`."""
    response = client.get(
        f"{CATALOG}/{catalog.ranked_repository_id + 999}/snapshots",
        headers=auth_headers(client, viewer),
    )
    assert response.status_code == 404
    assert response.json()["code"] == "NOT_FOUND"
