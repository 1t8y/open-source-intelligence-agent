"""The demo seeder has to be idempotent and honest about its two boundary shapes.

`scripts/seed_demo.py` turns an empty database into a demonstrable state. Two properties are worth
pinning in a test rather than trusting to a manual run:

1. **Idempotency** — every entity is looked up before it is created, so a second run must not add
   a single row. The seeder is meant to be re-run against a live deployment, and a seeder that
   duplicates on re-run is a bug waiting to inflate a candidate budget.

2. **The boundary shapes are real** — the T25 evaluation dataset keeps two cases disabled because
   this deployment cannot yet produce their data shapes. The seeder is what produces them:
   `example/readme-missing` must have *no* `repository_contents` row, and
   `example/prompt-injection` must carry instruction-shaped text in its cached README. If either
   drifts, the evaluation would re-enable a case against data that does not actually exercise the
   boundary it names.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

import scripts.seed_demo as seed
from open_source_intelligence.persistence.models.intelligence import (
    DataSource,
    Repository,
    RepositoryContent,
    RepoSnapshot,
    TopicRepository,
)
from open_source_intelligence.persistence.models.notifications import PushTarget


def _count(session: Session, model: type) -> int:
    """Return the row count for one model."""
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


def test_seeding_is_idempotent(session: Session) -> None:
    """A second pass over the same database adds no rows."""
    seed._ensure_accounts(  # noqa: SLF001
        session,
        admin_password="admin-password-123",
        user_password="user-password-123",
    )
    admin_user = seed.get_user_by_username(session, "admin")
    assert admin_user is not None
    source = seed._ensure_source(session)  # noqa: SLF001
    topic = seed._ensure_topic(session, admin_user)  # noqa: SLF001

    seed._ensure_repositories(session, source, topic)  # noqa: SLF001
    seed._ensure_push_targets(session)  # noqa: SLF001

    repos = _count(session, Repository)
    snapshots = _count(session, RepoSnapshot)
    contents = _count(session, RepositoryContent)
    links = _count(session, TopicRepository)
    targets = _count(session, PushTarget)

    # Second pass: nothing may change.
    seed._ensure_repositories(session, source, topic)  # noqa: SLF001
    seed._ensure_push_targets(session)  # noqa: SLF001
    session.commit()

    assert _count(session, Repository) == repos
    assert _count(session, RepoSnapshot) == snapshots
    assert _count(session, RepositoryContent) == contents
    assert _count(session, TopicRepository) == links
    assert _count(session, PushTarget) == targets


def test_boundary_shapes_are_real(session: Session) -> None:
    """The two disabled evaluation cases have to be produced as documented data shapes."""
    admin = seed.get_user_by_username(session, "admin")
    if admin is None:
        seed._ensure_accounts(  # noqa: SLF001
            session,
            admin_password="admin-password-123",
            user_password="user-password-123",
        )
        admin = seed.get_user_by_username(session, "admin")
    assert admin is not None
    source = seed._ensure_source(session)  # noqa: SLF001
    topic = seed._ensure_topic(session, admin)  # noqa: SLF001
    seed._ensure_repositories(session, source, topic)  # noqa: SLF001

    missing = session.scalar(
        select(Repository).where(Repository.full_name == "example/readme-missing")
    )
    assert missing is not None
    assert (
        session.scalar(
            select(RepositoryContent).where(RepositoryContent.repository_id == missing.id)
        )
        is None
    )

    injected = session.scalar(
        select(Repository).where(Repository.full_name == "example/prompt-injection")
    )
    assert injected is not None
    content = session.scalar(
        select(RepositoryContent).where(RepositoryContent.repository_id == injected.id)
    )
    assert content is not None
    assert "Ignore all previous instructions" in content.content
    assert "忽略之前的所有指令" in content.content


def test_seed_writes_the_full_loop_shape(session: Session) -> None:
    """The seeder has to land a topic, ranked candidates, snapshots, and a data source."""
    admin = seed.get_user_by_username(session, "admin")
    if admin is None:
        seed._ensure_accounts(  # noqa: SLF001
            session,
            admin_password="admin-password-123",
            user_password="user-password-123",
        )
        admin = seed.get_user_by_username(session, "admin")
    assert admin is not None
    source = seed._ensure_source(session)  # noqa: SLF001
    topic = seed._ensure_topic(session, admin)  # noqa: SLF001
    seed._ensure_repositories(session, source, topic)  # noqa: SLF001

    assert source.source_type == "github"
    assert topic.name == "sweep-llm-agent-topic"
    assert topic.enabled is True

    links = session.scalars(
        select(TopicRepository)
        .where(TopicRepository.topic_id == topic.id)
        .order_by(TopicRepository.rank)
    ).all()
    assert [link.rank for link in links] == [1, 2, 3, 4, 5]
    # The three normal candidates are research targets; the two boundary shapes are not.
    assert [link.selected_for_agent for link in links] == [True, True, True, False, False]

    # Every candidate has seven snapshots (a per-day series, not a before/after pair).
    for repo in session.scalars(select(Repository)).all():
        snap = session.scalars(
            select(RepoSnapshot).where(RepoSnapshot.repository_id == repo.id)
        ).all()
        assert len(snap) == 7


def test_collected_repository_is_bound_not_rewritten(session: Session) -> None:
    """A repository the collector owns keeps its own counters, history, and README.

    This is the mixed-database defect stated as a test. The seeder used to look a candidate up by
    `provider_repo_id`, which it writes as `owner/name`; a collected GitHub row carries the
    numeric provider id instead, so the lookup missed and the seeder inserted a *second* row for
    the same project, with a fabricated star series and a fabricated snapshot history. The console
    then showed two different star counts for `morluto/rea` — 370,108 collected and 285,395
    seeded — and a topic that ranked both.
    """
    admin = seed.get_user_by_username(session, "admin")
    if admin is None:
        seed._ensure_accounts(  # noqa: SLF001
            session,
            admin_password="admin-password-123",
            user_password="user-password-123",
        )
        admin = seed.get_user_by_username(session, "admin")
    assert admin is not None
    source = seed._ensure_source(session)  # noqa: SLF001
    topic = seed._ensure_topic(session, admin)  # noqa: SLF001

    collected = Repository(
        source_id=source.id,
        provider_repo_id="1209966933",
        full_name="morluto/rea",
        html_url="https://github.com/morluto/rea",
        description="collected",
        language="Python",
        stars=370108,
        forks=3120,
        first_seen_at=datetime(2026, 10, 20, tzinfo=UTC),
        last_seen_at=datetime(2026, 10, 23, tzinfo=UTC),
    )
    session.add(collected)
    session.flush()
    session.add(
        RepositoryContent(
            repository_id=collected.id,
            content_type="readme",
            source_url="https://github.com/morluto/rea/blob/main/README.md",
            content="# rea\n\nCollected README body.",
            content_hash="collected",
        )
    )
    for offset in range(4):
        session.add(
            RepoSnapshot(
                repository_id=collected.id,
                snapshot_date=date(2026, 10, 20) + timedelta(days=offset),
                stars=370100 + offset,
                forks=3120,
                stars_delta=1,
                forks_delta=0,
            )
        )
    session.commit()

    seed._ensure_repositories(session, source, topic)  # noqa: SLF001
    session.commit()

    rows = session.scalars(select(Repository).where(Repository.full_name == "morluto/rea")).all()
    assert len(rows) == 1, "the seeder created a second row for one repository"
    assert rows[0].id == collected.id
    assert rows[0].provider_repo_id == "1209966933"
    assert rows[0].stars == 370108, "the fixture series overwrote the collected counter"

    snapshots = session.scalars(
        select(RepoSnapshot).where(RepoSnapshot.repository_id == collected.id)
    ).all()
    assert len(snapshots) == 4, "the seeder wrote fixture snapshots over a collected series"

    content = session.scalar(
        select(RepositoryContent).where(RepositoryContent.repository_id == collected.id)
    )
    assert content is not None
    assert content.content == "# rea\n\nCollected README body."

    link = session.get(TopicRepository, (topic.id, collected.id))
    assert link is not None
    assert link.selected_for_agent is True
    assert link.manually_pinned is True


def test_seeding_leaves_no_shared_rank(session: Session) -> None:
    """A leftover candidate must not hold a position the fixture also holds.

    A ranking pass leaves the topic carrying the repositories it scored, ranked from 1. Seeding
    then writes ranks 1..5 for its own candidates, so a leftover that happens to sit at 2 gives the
    topic two candidates at position 2 — and every reader that orders by rank (the candidate list,
    the report's provenance block, the Agent's candidate loader) then orders them arbitrarily.
    """
    admin = seed.get_user_by_username(session, "admin")
    if admin is None:
        seed._ensure_accounts(  # noqa: SLF001
            session,
            admin_password="admin-password-123",
            user_password="user-password-123",
        )
        admin = seed.get_user_by_username(session, "admin")
    assert admin is not None
    source = seed._ensure_source(session)  # noqa: SLF001
    topic = seed._ensure_topic(session, admin)  # noqa: SLF001
    stale = Repository(
        source_id=source.id,
        provider_repo_id="555000111",
        full_name="example/leftover",
        html_url="https://github.com/example/leftover",
        description="leftover",
        language="Python",
        stars=10,
        forks=1,
        first_seen_at=datetime(2026, 10, 20, tzinfo=UTC),
        last_seen_at=datetime(2026, 10, 23, tzinfo=UTC),
    )
    session.add(stale)
    session.flush()
    session.add(
        TopicRepository(
            topic_id=topic.id,
            repository_id=stale.id,
            score=Decimal("1"),
            rank=2,
            selected_for_agent=True,
            manually_pinned=False,
        )
    )
    session.commit()

    seed._ensure_repositories(session, source, topic)  # noqa: SLF001
    session.commit()

    ranks = session.scalars(
        select(TopicRepository.rank).where(TopicRepository.topic_id == topic.id)
    ).all()
    leftover = session.get(TopicRepository, (topic.id, stale.id))

    assert len(ranks) == len(set(ranks)), f"two candidates share a position: {sorted(ranks)}"
    assert leftover is not None
    assert leftover.rank > len(seed.REPOSITORIES)


def test_accounts_cover_all_three_roles(session: Session) -> None:
    """The seeder has to produce an admin, an editor, and a viewer."""
    seed._ensure_accounts(  # noqa: SLF001
        session,
        admin_password="admin-password-123",
        user_password="user-password-123",
    )
    for username in ("admin", "editor", "viewer"):
        assert seed.get_user_by_username(session, username) is not None
    # The data source is created once, not per account.
    assert _count(session, DataSource) == 0
