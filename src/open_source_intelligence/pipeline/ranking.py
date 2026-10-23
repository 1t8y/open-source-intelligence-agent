"""Rule-based topic candidate ranking over snapshot history."""

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from open_source_intelligence.persistence.models.intelligence import (
    Repository,
    RepoSnapshot,
    Topic,
    TopicRepository,
)
from open_source_intelligence.security.audit import record_audit_event


@dataclass(frozen=True, slots=True)
class RankedCandidate:
    """One ranked repository and its computed score."""

    repository_id: int
    rank: int
    score: Decimal
    selected_for_agent: bool


def _matches_topic(repository: Repository, topic: Topic) -> bool:
    """Apply deterministic language, keyword, and star filters."""
    if repository.stars < topic.min_stars:
        return False
    languages = topic.languages_json or []
    if languages and repository.language not in languages:
        return False
    keywords = [keyword.lower() for keyword in (topic.keywords_json or [])]
    if keywords:
        topics = repository.topics_json or []
        haystack = " ".join(
            [
                repository.full_name,
                repository.description or "",
                *[str(topic_name) for topic_name in topics],
            ]
        ).lower()
        if not any(keyword in haystack for keyword in keywords):
            return False
    return True


def _window_growth(
    snapshots: list[RepoSnapshot],
) -> tuple[int, int, int]:
    """Return star growth, fork growth, and latest stars for a window."""
    if not snapshots:
        return 0, 0, 0
    ordered = sorted(snapshots, key=lambda snapshot: snapshot.snapshot_date)
    first = ordered[0]
    latest = ordered[-1]
    if len(ordered) == 1:
        star_growth = first.stars_delta or 0
        fork_growth = first.forks_delta or 0
    else:
        star_growth = latest.stars - first.stars
        fork_growth = latest.forks - first.forks
    return star_growth, fork_growth, latest.stars


def rank_topic_candidates(
    session: Session,
    topic: Topic,
    *,
    as_of_date: date,
) -> list[RankedCandidate]:
    """Rank and persist current candidate links for one topic."""
    start_date = as_of_date - timedelta(days=max(topic.window_days - 1, 0))
    repositories = session.scalars(select(Repository).order_by(Repository.id)).all()
    candidates: list[tuple[Repository, Decimal]] = []
    for repository in repositories:
        if not _matches_topic(repository, topic):
            continue
        snapshots = list(
            session.scalars(
                select(RepoSnapshot).where(
                    RepoSnapshot.repository_id == repository.id,
                    RepoSnapshot.snapshot_date >= start_date,
                    RepoSnapshot.snapshot_date <= as_of_date,
                )
            ).all()
        )
        if not snapshots:
            continue
        star_growth, fork_growth, latest_stars = _window_growth(snapshots)
        score = (
            Decimal(star_growth)
            + Decimal(fork_growth) * Decimal("2")
            + Decimal(latest_stars) / Decimal("1000")
        )
        score = max(Decimal("0"), score)
        candidates.append((repository, score))

    candidates.sort(key=lambda item: (-item[1], -item[0].stars, item[0].full_name))

    # Refresh in place instead of rebuilding the topic's candidate list. A blind
    # delete-then-insert loses two things the ranker cannot know about: links whose repository
    # has no snapshot inside this window (nothing to score, but still a deliberate candidate)
    # and links a human pinned by hand — an evaluation topic holds exactly one of those, so one
    # daily pass over an empty ranking would empty the case it exists to exercise.
    existing = {
        link.repository_id: link
        for link in session.scalars(
            select(TopicRepository).where(TopicRepository.topic_id == topic.id)
        ).all()
    }
    ranked_ids = {repository.id for repository, _ in candidates}
    for repository_id, existing_link in existing.items():
        if repository_id in ranked_ids or existing_link.manually_pinned:
            continue
        # The repository was scored and no longer qualifies, so the link goes away. A pinned
        # link is exempt: it was not the ranker's to remove.
        session.delete(existing_link)

    ranked: list[RankedCandidate] = []
    for rank, (repository, score) in enumerate(candidates, start=1):
        selected = rank <= topic.max_candidates
        link = existing.get(repository.id)
        if link is None:
            session.add(
                TopicRepository(
                    topic_id=topic.id,
                    repository_id=repository.id,
                    score=score,
                    rank=rank,
                    selected_for_agent=selected,
                    manually_pinned=False,
                )
            )
        else:
            link.score = score
            link.rank = rank
            # A pinned candidate stays selected wherever it lands in the ranking, which is what
            # "pinned" means; an unpinned one follows the cut-off as before.
            link.selected_for_agent = True if link.manually_pinned else selected
            selected = link.selected_for_agent
        ranked.append(
            RankedCandidate(
                repository_id=repository.id,
                rank=rank,
                score=score,
                selected_for_agent=selected,
            )
        )
    record_audit_event(
        session,
        action="candidate.rank",
        resource_type="topic",
        resource_id=str(topic.id),
        result="success",
        metadata_json={"candidate_count": len(ranked), "as_of_date": as_of_date.isoformat()},
    )
    session.commit()
    return ranked


def rank_all_topics(session: Session, *, as_of_date: date) -> dict[int, int]:
    """Rank candidates for all enabled topics."""
    topics = session.scalars(select(Topic).where(Topic.enabled.is_(True))).all()
    return {
        topic.id: len(rank_topic_candidates(session, topic, as_of_date=as_of_date))
        for topic in topics
    }
