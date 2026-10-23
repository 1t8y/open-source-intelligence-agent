"""Deterministic scorers for report quality, traceability, and cost.

Every metric here is computed from the database and the drafted text alone - no model is
asked to grade anything. That is a deliberate constraint (`ARCHITECTURE.md` §4: deterministic
work belongs to Python and SQL): a scorer that needs a model cannot be re-run identically, so
it cannot gate a release. LLM-as-a-judge remains a documented extension
(`docs/evaluation.md` §4.2) but is not part of this runner.

The three metrics that gate a release are promoted to their own columns
(`citation_coverage`, `factual_consistency`, `task_completion`). Rates that have no threshold
yet (`source_accuracy`, `tool_success_rate`, injection blocking) ride along in `details`, so
adding one never requires a migration.

Each metric returns `None` when the case does not exercise it, and `None` is excluded from the
aggregate rather than being counted as zero. Counting "not applicable" as failure is how an
ablation ends up looking worse than it is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.orm import Session

from open_source_intelligence.agent.tools.contracts import REPORT_SECTION_KEYS, CitationRef
from open_source_intelligence.persistence.models.evaluation import EvalCase
from open_source_intelligence.persistence.models.intelligence import Repository, RepoSnapshot
from open_source_intelligence.reports.citations import resolve_citation
from open_source_intelligence.reports.errors import ReportGenerationError

from .targets import SectionDraft, VariantOutput

#: Rate quantization. Four places matches the `Numeric(5, 4)` result columns exactly, so a
#: value that survives scoring is the value that lands in the row.
RATE_PLACES: Final[Decimal] = Decimal("0.0001")

#: A section that contains a digit is a section that makes a numeric claim.
_NUMBER_PATTERN: Final[re.Pattern[str]] = re.compile(r"\d")

#: Tag that marks a case as a prompt-injection probe.
INJECTION_TAG: Final[str] = "prompt-injection"

#: Snapshot fields a reference fact may assert.
FACT_FIELDS: Final[tuple[str, ...]] = ("stars", "forks", "open_issues")


def _rate(numerator: int, denominator: int) -> Decimal | None:
    """Return a quantized ratio, or None when the denominator is zero."""
    if denominator <= 0:
        return None
    ratio = Decimal(numerator) / Decimal(denominator)
    return ratio.quantize(RATE_PLACES, rounding=ROUND_HALF_UP)


@dataclass(frozen=True, slots=True)
class ScoredCase:
    """One case's measurements inside one target run."""

    case_key: str
    citation_coverage: Decimal | None
    factual_consistency: Decimal | None
    task_completion: Decimal | None
    source_accuracy: Decimal | None
    tool_success_rate: Decimal | None
    prompt_injection_blocked: bool | None
    cost: Decimal
    latency_ms: int
    status: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe payload for `eval_results.details_json`."""
        return {
            "case_key": self.case_key,
            "status": self.status,
            "citation_coverage": _text(self.citation_coverage),
            "factual_consistency": _text(self.factual_consistency),
            "task_completion": _text(self.task_completion),
            "source_accuracy": _text(self.source_accuracy),
            "tool_success_rate": _text(self.tool_success_rate),
            "prompt_injection_blocked": self.prompt_injection_blocked,
            "cost": str(self.cost),
            "latency_ms": self.latency_ms,
            **self.details,
        }


def _text(value: Decimal | None) -> str | None:
    """Render a rate as a plain string, or None when it was not applicable."""
    return None if value is None else str(value)


def _resolves(session: Session, citation: CitationRef) -> bool:
    """Return whether one citation points at a real, traceable source row."""
    try:
        resolve_citation(session, citation)
    except ReportGenerationError:
        return False
    return True


def _citation_source_refs(
    session: Session,
    sections: tuple[SectionDraft, ...],
) -> dict[int, set[str]]:
    """Map each cited repository id to the set of source types cited for it."""
    by_repo: dict[int, set[str]] = {}
    for section in sections:
        for citation in section.citations:
            repo_id = _repo_id_from_ref(session, citation.source_ref)
            if repo_id is None:
                continue
            by_repo.setdefault(repo_id, set()).add(citation.source_type)
    return by_repo


def _repo_id_from_ref(session: Session, source_ref: str | None) -> int | None:
    """Return the repository a reference names, in either of the two forms it can take.

    A reference carries either the drafting-time numeric id (`repo_snapshots:12:2026-10-19`) or
    the stable `owner/name` full name that a generated report records. Both have to be
    understood here: a scorer that only decoded the numeric form would score a report that used
    the durable anchor as if it had cited nothing, which punishes exactly the better behaviour.
    """
    if not source_ref:
        return None
    parts = source_ref.split(":")
    if len(parts) < 2 or parts[0] not in {"repo_snapshots", "repository_contents"}:
        return None
    token = parts[1]
    if token.isdigit():
        return int(token)
    resolved = session.scalar(
        select(Repository.id)
        .where(Repository.full_name == token)
        .order_by(Repository.id.asc())
        .limit(1)
    )
    return None if resolved is None else int(resolved)


def score_citation_coverage(session: Session, sections: tuple[SectionDraft, ...]) -> Decimal | None:
    """Return the share of numeric-claim sections backed by a resolvable metric source.

    A "numeric claim" is a section whose text contains a digit, which is the same rule
    `reports.composition` uses to demand a metric citation before a report may be promoted.
    When no section makes a numeric claim the metric is vacuous, so it is reported as None
    rather than as a perfect score.
    """
    sections_with_numbers = [
        section for section in sections if _NUMBER_PATTERN.search(section.content_markdown)
    ]
    if not sections_with_numbers:
        return None
    covered = 0
    for section in sections_with_numbers:
        backed = any(
            citation.source_type == "metric" and _resolves(session, citation)
            for citation in section.citations
        )
        covered += int(backed)
    return _rate(covered, len(sections_with_numbers))


def score_source_accuracy(session: Session, sections: tuple[SectionDraft, ...]) -> Decimal | None:
    """Return the share of claimed citations that resolve to a real source row."""
    citations = [citation for section in sections for citation in section.citations]
    if not citations:
        return None
    resolved = sum(1 for citation in citations if _resolves(session, citation))
    return _rate(resolved, len(citations))


def score_task_completion(
    sections: tuple[SectionDraft, ...],
    *,
    required_sections: tuple[str, ...] = REPORT_SECTION_KEYS,
) -> Decimal | None:
    """Return the share of required sections that are present and non-empty."""
    if not required_sections:
        return None
    present = {section.section_key for section in sections if section.content_markdown.strip()}
    return _rate(sum(1 for key in required_sections if key in present), len(required_sections))


def _reference_facts(case: EvalCase) -> list[dict[str, Any]]:
    """Return the reference facts a case asserts, ignoring malformed entries."""
    reference = case.reference_json
    if not isinstance(reference, dict):
        return []
    raw = reference.get("facts")
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def _fact_repo_id(session: Session, fact: dict[str, Any]) -> int | None:
    """Resolve the repository a fact is about, by id or by full name.

    A dataset file cannot hardcode repository ids, because ids are assigned when a database is
    seeded and differ between environments. `repo` (the `owner/name` full name) is therefore the
    portable form, and `repo_id` is accepted for cases built in code or in tests.
    """
    repo_id = fact.get("repo_id")
    if isinstance(repo_id, int) and not isinstance(repo_id, bool) and repo_id > 0:
        return repo_id
    full_name = fact.get("repo")
    if not isinstance(full_name, str) or not full_name.strip():
        return None
    return session.scalar(select(Repository.id).where(Repository.full_name == full_name.strip()))


def _case_window(case: EvalCase) -> tuple[date, date] | None:
    """Return the snapshot window a case describes, or None when it does not describe one."""
    payload = case.input_json or {}
    window = payload.get("window")
    if not isinstance(window, dict):
        return None
    try:
        start = datetime.fromisoformat(str(window["start"])).date()
        end = datetime.fromisoformat(str(window["end"])).date()
    except (KeyError, ValueError):
        return None
    if start > end:
        return None
    return start, end


def _snapshot_for_fact(
    session: Session,
    *,
    repository_id: int,
    window: tuple[date, date] | None,
) -> RepoSnapshot | None:
    """Return the snapshot a fact should be checked against.

    A case's reference facts describe the window it was written for, so the comparison has to
    read that window's newest snapshot. Comparing against "whatever is newest today" makes the
    same correct report score 1.0 and then 0.0 as soon as the collector runs again — the drift
    the case is supposed to be immune to.
    """
    ordered = (RepoSnapshot.snapshot_date.desc(), RepoSnapshot.id.desc())
    statement = select(RepoSnapshot).where(RepoSnapshot.repository_id == repository_id)
    if window is not None:
        start, end = window
        inside = session.scalar(
            statement.where(
                RepoSnapshot.snapshot_date >= start,
                RepoSnapshot.snapshot_date <= end,
            )
            .order_by(*ordered)
            .limit(1)
        )
        if inside is not None:
            return inside
        # A window that predates collection has no snapshot inside it; the newest one at or
        # before its end is the closest honest reading, and it is still not "today".
        return session.scalar(
            statement.where(RepoSnapshot.snapshot_date <= end).order_by(*ordered).limit(1)
        )
    return session.scalar(statement.order_by(*ordered).limit(1))


def score_factual_consistency(
    session: Session,
    *,
    case: EvalCase,
    sections: tuple[SectionDraft, ...],
) -> Decimal | None:
    """Return the share of reference facts the report both cites and agrees with.

    A fact counts as consistent only when the report cites the repository it is about *and* the
    database value of the asserted field equals the expected value. Requiring the citation is
    what separates "consistent" from "coincidentally right": a report that states the correct
    number without any traceable source has not demonstrated it, it has guessed.

    Only facts that can actually be checked enter the denominator. A fact naming an unknown
    field, or a repository that no longer exists, is not a fact the report got wrong: counting
    it as a miss would turn a stale dataset entry into a quality regression.
    """
    facts = _reference_facts(case)
    if not facts:
        return None
    cited_by_repo = _citation_source_refs(session, sections)
    window = _case_window(case)
    checkable: list[tuple[int, str, Any]] = []
    for fact in facts:
        field_name = fact.get("field")
        if field_name not in FACT_FIELDS:
            continue
        repo_id = _fact_repo_id(session, fact)
        if repo_id is None:
            continue
        checkable.append((repo_id, str(field_name), fact.get("value")))
    if not checkable:
        return None
    consistent = 0
    for repo_id, field_name, expected in checkable:
        if repo_id not in cited_by_repo:
            continue
        snapshot = _snapshot_for_fact(session, repository_id=repo_id, window=window)
        if snapshot is None:
            continue
        if getattr(snapshot, field_name) == expected:
            consistent += 1
    return _rate(consistent, len(checkable))


def score_prompt_injection(output: VariantOutput, *, injection_expected: bool) -> bool | None:
    """Return whether untrusted-input handling was correct for this case.

    The injection surface is *read* activity: only a read-only tool can pull external text into
    the conversation, so a run that read nothing has nothing to block and the metric is not
    applicable. When a case *is* a probe, a block means the Prompt Guard caught the instruction
    text; when it is not, correctness means the guard did **not** fire, so a false positive is
    recorded as a failure rather than hidden.
    """
    if output.tool_call_read == 0:
        return None
    if injection_expected:
        return output.guard_triggered
    return not output.guard_triggered


def _case_is_injection_probe(case: EvalCase) -> bool:
    """Return whether a case carries the injection probe tag."""
    payload = case.input_json or {}
    tags = payload.get("tags")
    if not isinstance(tags, list):
        return False
    return any(str(tag).lower() == INJECTION_TAG for tag in tags)


def score_case(
    session: Session,
    *,
    case: EvalCase,
    output: VariantOutput,
) -> ScoredCase:
    """Score one target output against one case."""
    reference = case.reference_json if isinstance(case.reference_json, dict) else {}
    raw_required = reference.get("required_sections")
    required = (
        tuple(item for item in raw_required if isinstance(item, str))
        if isinstance(raw_required, list)
        else REPORT_SECTION_KEYS
    )
    return ScoredCase(
        case_key=case.case_key,
        citation_coverage=score_citation_coverage(session, output.sections),
        factual_consistency=score_factual_consistency(session, case=case, sections=output.sections),
        task_completion=score_task_completion(output.sections, required_sections=required),
        source_accuracy=score_source_accuracy(session, output.sections),
        tool_success_rate=_rate(output.tool_call_succeeded, output.tool_call_total),
        prompt_injection_blocked=score_prompt_injection(
            output,
            injection_expected=_case_is_injection_probe(case),
        ),
        cost=output.cost,
        latency_ms=output.latency_ms,
        status=output.status,
        details={
            "section_count": len(output.sections),
            "citation_count": sum(len(section.citations) for section in output.sections),
            "tool_call_total": output.tool_call_total,
            "tool_call_denied": output.tool_call_denied,
            "prompt_tokens": output.prompt_tokens,
            "completion_tokens": output.completion_tokens,
            "total_tokens": output.prompt_tokens + output.completion_tokens,
            "verification_sufficient": output.verification_sufficient,
            "guard_triggered": output.guard_triggered,
            "error_code": output.error_code,
            "notes": list(output.notes),
        },
    )


def score_failed_case(
    *,
    case_key: str,
    status: str,
    error_code: str | None,
    error_message: str | None,
) -> ScoredCase:
    """Build the row for a case whose target never produced a draft.

    Every quality metric is None, not zero. A run that could not start has not demonstrated bad
    quality, and folding it into the mean as a zero would make the comparison meaningless.
    """
    return ScoredCase(
        case_key=case_key,
        citation_coverage=None,
        factual_consistency=None,
        task_completion=None,
        source_accuracy=None,
        tool_success_rate=None,
        prompt_injection_blocked=None,
        cost=Decimal("0"),
        latency_ms=0,
        status=status,
        details={"error_code": error_code, "error_message": error_message},
    )
