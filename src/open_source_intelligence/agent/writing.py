"""Parse the model's draft response into validated report sections.

The model is asked for a JSON document, but a real model occasionally ignores the shape and
answers in plain Markdown. Both are accepted here: JSON is parsed first (it carries the
citations the traceability checks need), and a Markdown fallback splits the answer on its
headings when no JSON object is present. The fallback is the "not fully formatted is fine"
contract — the report stays concise and readable, and a section that carries no citation is
flagged for human review rather than blowing up the whole run.

Field names mirror `SaveReportSectionInput` in `agent/tools/contracts.py`;
`tests/agent/test_writing.py` pins that alignment so the two contracts cannot drift apart.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from open_source_intelligence.agent.tools.contracts import (
    MAX_CITATIONS,
    MAX_SECTION_CHARS,
    REPORT_SECTION_KEYS,
    CitationRef,
)

_FENCE_PATTERN = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)

#: Chinese / English heading the model may use to start a section, mapped to the canonical key.
_SECTION_HEADINGS: dict[str, str] = {
    "summary": "summary",
    "摘要": "summary",
    "本周摘要": "summary",
    "trend": "trend",
    "趋势": "trend",
    "趋势观察": "trend",
    "highlights": "highlights",
    "亮点": "highlights",
    "本周亮点": "highlights",
    "risks": "risks",
    "风险": "risks",
    "风险提示": "risks",
    "recommendation": "recommendation",
    "建议": "recommendation",
    "行动建议": "recommendation",
    "appendix": "appendix",
    "附录": "appendix",
}
_HEADING_PATTERN = re.compile(r"^#{1,4}\s*(.+?)\s*$", re.MULTILINE)


class DraftPayloadError(ValueError):
    """Raised when a model draft response cannot be validated."""


class DraftSection(BaseModel):
    """One validated draft section produced by the model.

    Deliberately tolerant (`extra="ignore"`): a model that adds a `report_meta` or `title`
    field is not a reason to reject an otherwise usable draft. Unknown fields are dropped and
    the known ones are kept, so the failure rate tracks the real content, not the envelope.
    """

    model_config = ConfigDict(extra="ignore")

    section_key: str
    content_markdown: str = Field(min_length=1, max_length=MAX_SECTION_CHARS)
    citations: list[CitationRef] = Field(default_factory=list, max_length=MAX_CITATIONS)

    @field_validator("section_key")
    @classmethod
    def validate_section_key(cls, value: str) -> str:
        """Only report template sections may be drafted."""
        if value not in REPORT_SECTION_KEYS:
            raise ValueError(f"section_key must be one of: {', '.join(REPORT_SECTION_KEYS)}")
        return value


class DraftPayload(BaseModel):
    """The top-level object a model response may contain."""

    model_config = ConfigDict(extra="ignore")

    sections: list[DraftSection]


def _json_candidates(text: str) -> tuple[str, ...]:
    """Return progressively more forgiving slices of the response that may hold JSON."""
    candidates = [text]
    fenced = _FENCE_PATTERN.search(text)
    if fenced is not None:
        candidates.append(fenced.group(1).strip())
    start = text.find("{")
    end = text.rfind("}")
    if 0 <= start < end:
        candidates.append(text[start : end + 1])
    return tuple(candidates)


def _extract_json(text: str) -> Any:
    """Return the first JSON value that parses out of a model response."""
    for candidate in _json_candidates(text.strip()):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise DraftPayloadError("model response did not contain a JSON object")


def _split_markdown_sections(text: str) -> list[DraftSection]:
    """Split a plain-Markdown answer into sections keyed by its headings.

    This is the fallback for a model that ignored the JSON contract and wrote a normal
    report. Each `## heading` becomes a section; a heading that maps to a known key is kept,
    and a block with no heading is attached to the first known section. Sections carry no
    citations, so the run is flagged for review by the verify stage rather than published
    with fabricated evidence.
    """
    stripped = text.strip()
    if not stripped:
        raise DraftPayloadError("model response was empty")

    # Find heading positions; anything before the first heading is a preamble to drop.
    matches = list(_HEADING_PATTERN.finditer(stripped))
    if not matches:
        # No headings at all: treat the whole answer as a single summary block.
        return [DraftSection(section_key="summary", content_markdown=stripped[:MAX_SECTION_CHARS])]

    sections: list[DraftSection] = []
    for index, match in enumerate(matches):
        heading = match.group(1).strip()
        key = _SECTION_HEADINGS.get(heading.lower())
        if key is None:
            continue
        end = matches[index + 1].start() if index + 1 < len(matches) else len(stripped)
        body = stripped[match.end() : end].strip()
        if not body:
            continue
        sections.append(DraftSection(section_key=key, content_markdown=body[:MAX_SECTION_CHARS]))
    return sections


def _dedupe_keep_first(sections: list[DraftSection]) -> list[DraftSection]:
    """Keep the first section per key, preserving order, so a repeated heading does not double."""
    seen: set[str] = set()
    ordered: list[DraftSection] = []
    for section in sections:
        if section.section_key in seen:
            continue
        seen.add(section.section_key)
        ordered.append(section)
    return ordered


def parse_draft_payload(text: str) -> list[DraftSection]:
    """Validate a model draft response and return one section per template slot.

    JSON is the primary contract and must contain each section exactly once. When the model
    answered in Markdown instead, the answer is split on headings; missing sections are left
    absent (the caller decides whether that is reviewable) rather than invented here.
    """
    try:
        raw = _extract_json(text)
    except DraftPayloadError:
        return _dedupe_keep_first(_split_markdown_sections(text))
    try:
        payload = DraftPayload.model_validate(raw)
    except ValidationError as exc:
        raise DraftPayloadError(
            f"draft payload failed validation: {exc.error_count()} error(s)"
        ) from exc
    keys = sorted(section.section_key for section in payload.sections)
    if keys != sorted(REPORT_SECTION_KEYS):
        raise DraftPayloadError(
            "draft must contain each report section exactly once: "
            f"expected {sorted(REPORT_SECTION_KEYS)}, got {keys}"
        )
    return list(payload.sections)
