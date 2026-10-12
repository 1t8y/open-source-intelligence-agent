"""Run T10 report composition and citation checks against MySQL."""

import json
import subprocess
import sys
from datetime import UTC, datetime
from typing import Any

import t8_mysql_smoke as t8
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from open_source_intelligence.agent.runtime import run_research_agent
from open_source_intelligence.agent.tools.truncation import content_digest
from open_source_intelligence.core.config import get_settings
from open_source_intelligence.persistence import models  # noqa: F401
from open_source_intelligence.persistence.models.reporting import (
    Report,
    ReportDraft,
    ReportSection,
    ReportSource,
    ReportVersion,
)
from open_source_intelligence.persistence.session import (
    create_database_engine,
    create_session_factory,
)
from open_source_intelligence.reports.composition import generate_report
from open_source_intelligence.reports.errors import ReportGenerationError

T10_NOW = datetime(2099, 5, 5, 12, 0, tzinfo=UTC)


def run_report_cli(agent_run_id: int) -> dict[str, Any]:
    """Generate one report through the real module CLI and parse its output."""
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "open_source_intelligence.reports.cli",
            "--agent-run-id",
            str(agent_run_id),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"report CLI failed: {completed.stderr.strip()}")
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _latest_section(session: Session, draft_id: int, section_key: str) -> ReportSection:
    section = session.scalar(
        select(ReportSection)
        .where(
            ReportSection.report_draft_id == draft_id,
            ReportSection.section_key == section_key,
        )
        .order_by(ReportSection.version.desc())
        .limit(1)
    )
    if section is None:
        raise RuntimeError(f"missing draft section: {section_key}")
    return section


def run_checks(session: Session, ids: dict[str, int]) -> dict[str, Any]:
    """Generate and validate a report from one real Agent draft."""
    settings = get_settings()
    agent_result = run_research_agent(
        session,
        topic_id=ids["topic_id"],
        settings=settings,
        now=t8.SMOKE_NOW,
    )
    if agent_result.report_draft_id is None:
        raise RuntimeError("Agent run did not create a report draft")

    first = generate_report(
        session,
        agent_run_id=agent_result.agent_run_id,
        now=T10_NOW,
    )
    second = run_report_cli(agent_result.agent_run_id)
    session.expire_all()
    report = session.get(Report, first.report_id)
    if report is None:
        raise RuntimeError("generated report was not persisted")
    versions_before_change = int(
        session.scalar(
            select(func.count(ReportVersion.id)).where(ReportVersion.report_id == report.id)
        )
        or 0
    )
    source_rows = session.scalars(
        select(ReportSource).where(ReportSource.report_id == report.id)
    ).all()
    metric_sources = [source for source in source_rows if source.source_type == "metric"]
    readme_sources = [source for source in source_rows if source.source_type == "readme"]

    section = _latest_section(session, agent_result.report_draft_id, "summary")
    section.content_markdown = section.content_markdown + "\n补充观察：本周新增证据已核对。"
    section.content_digest = content_digest(section.content_markdown)
    session.commit()
    changed = generate_report(
        session,
        agent_run_id=agent_result.agent_run_id,
        now=T10_NOW,
    )

    invalid_rejected = False
    section = _latest_section(session, agent_result.report_draft_id, "summary")
    section.citations_json = [
        {
            "source_type": "metric",
            "source_ref": "repo_snapshots:999999:2099-01-01",
            "source_url": None,
        }
    ]
    session.commit()
    try:
        generate_report(session, agent_run_id=agent_result.agent_run_id, now=T10_NOW)
    except ReportGenerationError:
        invalid_rejected = True

    session.expire_all()
    report = session.get(Report, first.report_id)
    if report is None:
        raise RuntimeError("report disappeared after invalid regeneration")
    version_rows = int(
        session.scalar(
            select(func.count(ReportVersion.id)).where(ReportVersion.report_id == report.id)
        )
        or 0
    )
    draft = session.get(ReportDraft, agent_result.report_draft_id)
    if draft is None:
        raise RuntimeError("report draft disappeared after generation")

    return {
        "agent_status": agent_result.status,
        "report_id": first.report_id,
        "report_status": report.status,
        "report_version": report.current_version,
        "first_created": first.created,
        "second_created": second["created"],
        "second_version_incremented": second["version_incremented"],
        "versions_before_change": versions_before_change,
        "version_rows": version_rows,
        "changed_version": changed.version,
        "changed_incremented": changed.version_incremented,
        "source_count": len(source_rows),
        "metric_source_count": len(metric_sources),
        "readme_source_count": len(readme_sources),
        "all_metric_snapshot_ids_resolved": all(
            source.metric_snapshot_id is not None for source in metric_sources
        ),
        "all_source_urls_https": all(
            source.source_url is not None and source.source_url.startswith("https://")
            for source in source_rows
        ),
        "invalid_citation_rejected": invalid_rejected,
        "draft_status": draft.status,
        "has_full_template": all(
            heading in report.content_markdown
            for heading in (
                "## 本周摘要",
                "## 趋势观察",
                "## 重点项目",
                "## 风险与不确定性",
                "## 行动建议",
                "## 附录",
                "## 引用与证据",
            )
        ),
    }


def main() -> None:
    """Run T10 MySQL smoke checks and print the result payload."""
    settings = get_settings()
    engine = create_database_engine(settings.database_url)
    session_factory = create_session_factory(engine)
    payload: dict[str, Any] = {}
    try:
        with session_factory() as session:
            t8.cleanup_verification_records(session)
            ids = t8.seed_topics(session)
            payload = run_checks(session, ids)
    finally:
        with session_factory() as session:
            t8.cleanup_verification_records(session)
        engine.dispose()
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
