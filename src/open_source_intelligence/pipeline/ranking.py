"""在快照历史上对主题候选做规则化排序。"""

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
    """一个已排序的仓库及其算出的得分，由 `rank_topic_candidates` 返回给调用方。"""

    repository_id: int
    rank: int
    score: Decimal
    selected_for_agent: bool


def _matches_topic(repository: Repository, topic: Topic) -> bool:
    """依次应用语言、关键词与 `stars` 阈值三类确定性筛选。"""
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
    """返回时间窗内的 `stars` 增长、`forks` 增长与最新 `stars`。"""
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
    """排序并持久化某个主题当前的候选关联。"""
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

    # 就地刷新，而不是重建主题的候选列表。盲目地先删后插会丢掉两样排序器无从知道的东西：
    # 仓库在这个窗口内没有快照的关联（无从打分，但仍是刻意保留的候选），以及人工手工置顶
    # 的关联 —— 一个评估主题里恰好只有一条这样的关联，所以对空排名跑一次每日任务，就会
    # 把那个用例本来要演练的东西清空。
    existing = {
        link.repository_id: link
        for link in session.scalars(
            select(TopicRepository).where(TopicRepository.topic_id == topic.id)
        ).all()
    }
    ranked_ids = {repository.id for repository, _ in candidates}
    dropped: set[int] = set()
    for repository_id, existing_link in existing.items():
        if repository_id in ranked_ids or existing_link.manually_pinned:
            continue
        # 这个仓库被打过分且不再满足条件，关联就此移除。置顶关联例外：它不归排序器删。
        session.delete(existing_link)
        dropped.add(repository_id)

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
            # 置顶候选无论排到第几名都保持被选中，这正是「置顶」的含义；未置顶的候选
            # 则照旧跟随截断线。
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

    # 落在本窗口打分范围之外的置顶关联仍保留旧的 `rank`，如今会与某个已打分候选的位次
    # 相撞：两行共用第 4 位，于是每个按 rank 排序的读者（候选列表、报告溯源、Agent 的
    # 候选加载器）在两者之间拿到的是任意顺序。把它们按原有相对顺序排到已打分候选之后，
    # 位次因此互不相同，排名也仍然表达它该表达的意思 —— 置顶意味着「被纳入」，而不是
    # 「被提上来」。
    tail = sorted(
        (
            (link.rank, repository_id)
            for repository_id, link in existing.items()
            if repository_id not in ranked_ids and repository_id not in dropped
        ),
        key=lambda item: (item[0], item[1]),
    )
    for offset, (_, repository_id) in enumerate(tail, start=1):
        existing[repository_id].rank = len(candidates) + offset

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
    """为所有启用主题排序候选。"""
    topics = session.scalars(select(Topic).where(Topic.enabled.is_(True))).all()
    return {
        topic.id: len(rank_topic_candidates(session, topic, as_of_date=as_of_date))
        for topic in topics
    }
