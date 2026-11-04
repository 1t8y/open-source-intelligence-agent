"""守护发布前的校验器，以及它落库的判定结果。"""

from __future__ import annotations

from collections.abc import Sequence

import pytest
from reports_support import SeededReport
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from support import NOW, build_settings

from open_source_intelligence.persistence.models.audit import AuditLog
from open_source_intelligence.persistence.models.reporting import (
    Report,
    ReportSource,
    ReportVerification,
)
from open_source_intelligence.reports.composition import SECTION_TITLES
from open_source_intelligence.reports.errors import ReportGenerationError
from open_source_intelligence.reports.verification import (
    REASON_CITATION_UNRESOLVED,
    REASON_LOW_CITATION_COVERAGE,
    REASON_NUMBER_WITHOUT_METRIC,
    REASON_SENSITIVE_CONTENT,
    REASON_SENSITIVITY_VISIBILITY_MISMATCH,
    REASON_STRUCTURE_MISSING_SECTIONS,
    VERDICT_PASSED,
    verify_report,
)

SUMMARY_TITLE = SECTION_TITLES["summary"]


def _sources(session: Session, report_id: int, section_key: str) -> Sequence[ReportSource]:
    return session.scalars(
        select(ReportSource).where(
            ReportSource.report_id == report_id,
            ReportSource.section_key == section_key,
        )
    ).all()


def test_verify_passes_and_is_idempotent(session: Session, report_case: SeededReport) -> None:
    """刚生成的报告能通过 A-004 的四项检查，重跑还复用同一行。"""
    outcome = verify_report(session, report_id=report_case.report_id, now=NOW)

    assert outcome.verdict == VERDICT_PASSED
    assert outcome.passed is True
    assert outcome.structure_ok is True
    assert outcome.citations_ok is True
    assert outcome.numbers_ok is True
    assert outcome.sensitivity_ok is True
    assert outcome.citation_coverage == 1.0
    assert outcome.reason_codes == ()

    again = verify_report(session, report_id=report_case.report_id, now=NOW)
    assert again.verdict == VERDICT_PASSED
    assert int(session.scalar(select(func.count()).select_from(ReportVerification)) or 0) == 1
    row = session.scalar(select(ReportVerification))
    assert row is not None
    assert row.verdict == VERDICT_PASSED
    assert row.version == 1

    audits = session.scalars(select(AuditLog).where(AuditLog.action == "report.verify")).all()
    assert len(audits) == 2
    assert all(event.result == "success" for event in audits)


def test_missing_section_heading_fails_structure(
    session: Session,
    report_case: SeededReport,
) -> None:
    """被截断的成稿要在任何人发布它之前就被拦下。"""
    report = report_case.reload(session)
    report.content_markdown = report.content_markdown.replace(f"## {SUMMARY_TITLE}", "## 摘要", 1)
    session.commit()

    outcome = verify_report(session, report_id=report_case.report_id, now=NOW)

    assert outcome.structure_ok is False
    assert outcome.verdict != VERDICT_PASSED
    assert REASON_STRUCTURE_MISSING_SECTIONS in outcome.reason_codes
    assert SUMMARY_TITLE in outcome.missing_headings


def test_unresolved_citation_is_detected(
    session: Session,
    report_case: SeededReport,
) -> None:
    """引用的来源行已经消失时，必须挡住发布。"""
    source = _sources(session, report_case.report_id, "summary")[0]
    source.source_type = "metric"
    source.source_ref = "repo_snapshots:999999:2099-01-01"
    source.metric_snapshot_id = None
    session.commit()

    outcome = verify_report(session, report_id=report_case.report_id, now=NOW)

    assert outcome.citations_ok is False
    assert REASON_CITATION_UNRESOLVED in outcome.reason_codes
    assert "repo_snapshots:999999:2099-01-01" in outcome.unresolved_refs


def test_number_without_metric_citation_is_detected(
    session: Session,
    report_case: SeededReport,
) -> None:
    """某一节写了数字、却只引了 README，不能算通过。"""
    repository_id = report_case.repositories[0].id
    report = report_case.reload(session)
    report.content_markdown = report.content_markdown.replace(
        f"## {SUMMARY_TITLE}\n",
        f"## {SUMMARY_TITLE}\n\n本周新增 42 star。\n",
        1,
    )
    for source in _sources(session, report_case.report_id, "summary"):
        source.source_type = "readme"
        source.source_ref = f"repository_contents:{repository_id}:readme"
        source.metric_snapshot_id = None
    session.commit()

    outcome = verify_report(session, report_id=report_case.report_id, now=NOW)

    assert outcome.numbers_ok is False
    assert REASON_NUMBER_WITHOUT_METRIC in outcome.reason_codes
    assert outcome.citations_ok is True


def test_credential_shaped_text_is_flagged_as_sensitive(
    session: Session,
    report_case: SeededReport,
) -> None:
    """共用的脱敏规则定义了什么样的内容不可接受。"""
    report = report_case.reload(session)
    report.content_markdown = report.content_markdown.replace(
        f"## {SUMMARY_TITLE}\n",
        f'## {SUMMARY_TITLE}\n\napi_key = "sk-abcdefghijklmnopqrst"\n',
        1,
    )
    session.commit()

    outcome = verify_report(session, report_id=report_case.report_id, now=NOW)

    assert outcome.sensitivity_ok is False
    assert outcome.sensitive_markers >= 1
    assert REASON_SENSITIVE_CONTENT in outcome.reason_codes


def test_confidential_report_must_not_be_internally_visible(
    session: Session,
    report_case: SeededReport,
) -> None:
    """机密报告不能在整个内网范围内可读。"""
    report = report_case.reload(session)
    report.sensitivity = "CONFIDENTIAL"
    report.visibility = "INTERNAL"
    session.commit()

    outcome = verify_report(session, report_id=report_case.report_id, now=NOW)

    assert outcome.sensitivity_ok is False
    assert REASON_SENSITIVITY_VISIBILITY_MISMATCH in outcome.reason_codes


def test_low_citation_coverage_is_flagged(
    session: Session,
    report_case: SeededReport,
) -> None:
    """删掉某一节的来源，会让覆盖率掉到配置的下限之下。"""
    for source in _sources(session, report_case.report_id, "appendix"):
        session.delete(source)
    session.commit()

    outcome = verify_report(session, report_id=report_case.report_id, now=NOW)

    assert outcome.citations_ok is False
    assert outcome.citation_coverage < 1.0
    assert REASON_LOW_CITATION_COVERAGE in outcome.reason_codes


def test_verification_honours_a_relaxed_coverage_floor(
    session: Session,
    report_case: SeededReport,
) -> None:
    """运维可以放低覆盖率下限，而不必牺牲其它检查。"""
    for source in _sources(session, report_case.report_id, "appendix"):
        session.delete(source)
    session.commit()

    outcome = verify_report(
        session,
        report_id=report_case.report_id,
        settings=build_settings(publish_min_citation_coverage=0.5),
        now=NOW,
    )

    assert REASON_LOW_CITATION_COVERAGE not in outcome.reason_codes
    assert outcome.citations_ok is False


def test_verification_rejects_an_unknown_report(session: Session) -> None:
    """给不存在的报告做校验是调用方的错误，不是悄悄通过。"""
    with pytest.raises(ReportGenerationError, match="does not exist"):
        verify_report(session, report_id=987654, now=NOW)
    assert int(session.scalar(select(func.count()).select_from(Report)) or 0) == 0
