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
