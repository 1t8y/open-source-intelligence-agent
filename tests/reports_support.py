"""Seeding helpers shared by the report, API, and web tests.

They sit next to `support.py` at the top of `tests/` because pytest puts `tests` on
`sys.path` (`pythonpath` in `pyproject.toml`), so every test directory can import them as a
plain top-level module. A sibling of `conftest.py` inside `tests/reports/` would only be
importable from that one directory.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from itertools import count

from sqlalchemy import select
from sqlalchemy.orm import Session
from support import NOW, Workspace, build_settings

from open_source_intelligence.agent.runtime import run_research_agent
from open_source_intelligence.persistence.models.intelligence import (
    DataSource,
    Repository,
    Topic,
)
from open_source_intelligence.persistence.models.reporting import Report
from open_source_intelligence.reports.composition import generate_report

FIRST_SNAPSHOT_DATE = date(2026, 10, 5)
LAST_SNAPSHOT_DATE = date(2026, 10, 11)

AUTO = "AUTO"
AUTO_WITH_DELAY = "AUTO_WITH_DELAY"
MANUAL = "MANUAL"

#: `topics.name` is unique, so every seeded topic needs its own label.
_TOPIC_SEQUENCE = count(1)
#: `repositories(source_id, provider_repo_id)` is unique, so ids must not repeat.
_REPO_SEQUENCE = count(1)


@dataclass(slots=True)
class SeededReport:
    """A generated report together with the run, topic, and repositories behind it."""

    report_id: int
    agent_run_id: int
    topic: Topic
    repositories: list[Repository] = field(default_factory=list)

    def reload(self, session: Session) -> Report:
        """Return the current report row."""
        report = session.get(Report, self.report_id)
        assert report is not None
        return report


def _shared_source(workspace: Workspace) -> DataSource:
    """Return the single GitHub source, creating it on first use.

    `data_sources.source_type` is unique — one row per provider — so every seeded topic
    attaches its repositories to the same source.
    """
    existing = workspace.session.scalar(select(DataSource).order_by(DataSource.id).limit(1))
    if existing is not None:
        return existing
    source = DataSource(
        name="github",
        source_type="github",
        base_url="https://github.example.com",
        enabled=True,
        rate_limit_per_minute=30,
    )
    workspace.session.add(source)
    workspace.session.flush()
    return source


def seed_topic(
    workspace: Workspace,
    *,
    publish_mode: str,
    repo_count: int = 2,
    name: str | None = None,
) -> tuple[Topic, list[Repository]]:
    """Create a topic with ranked candidates, a metric series, and cached READMEs."""
    topic = workspace.add_topic(name=name or f"AI Agent {next(_TOPIC_SEQUENCE)}")
    topic.publish_mode = publish_mode
    source = _shared_source(workspace)
    repositories: list[Repository] = []
    for index in range(repo_count):
        sequence = next(_REPO_SEQUENCE)
        repository = workspace.add_repository(
            source_id=source.id,
            provider_repo_id=str(sequence),
            full_name=f"acme/repo-{sequence}",
            stars=100 + index,
        )
        workspace.link(topic.id, repository.id, rank=index + 1)
        workspace.add_snapshot(repository.id, FIRST_SNAPSHOT_DATE, stars=100 + index, forks=10)
        workspace.add_snapshot(repository.id, LAST_SNAPSHOT_DATE, stars=140 + index, forks=12)
        workspace.add_content(
            repository.id,
            content="# README\nA cached repository readme used by the publishing tests.",
        )
        repositories.append(repository)
    workspace.session.flush()
    return topic, repositories


def run_agent_once(session: Session, topic_id: int) -> int:
    """Run the real deterministic Agent once and return the Agent run id."""
    result = run_research_agent(
        session,
        topic_id=topic_id,
        settings=build_settings(),
        now=NOW,
    )
    assert result.status == "completed"
    return result.agent_run_id


def build_report(
    session: Session,
    workspace: Workspace,
    *,
    publish_mode: str,
    repo_count: int = 2,
) -> SeededReport:
    """Seed a topic, run the Agent, and promote the draft into a report."""
    topic, repositories = seed_topic(
        workspace,
        publish_mode=publish_mode,
        repo_count=repo_count,
    )
    agent_run_id = run_agent_once(session, topic.id)
    generated = generate_report(session, agent_run_id=agent_run_id, now=NOW)
    return SeededReport(
        report_id=generated.report_id,
        agent_run_id=agent_run_id,
        topic=topic,
        repositories=repositories,
    )


def seed_published_sibling(session: Session, case: SeededReport) -> Report:
    """Insert an already-published sibling so the topic is no longer a first run.

    It reuses the report's own Agent run, which keeps the model/prompt/tool fingerprint
    identical — otherwise `MODEL_OR_PROMPT_CHANGED` would mask the behaviour under test.
    """
    sibling = Report(
        agent_run_id=case.agent_run_id,
        topic_id=case.topic.id,
        title="上周周报",
        current_version=1,
        content_markdown="# 上周周报\n",
        status="published",
        visibility="INTERNAL",
        sensitivity="INTERNAL",
        publish_mode="AUTO_WITH_DELAY",
        generated_at=NOW,
        published_at=NOW,
    )
    session.add(sibling)
    session.commit()
    return sibling
