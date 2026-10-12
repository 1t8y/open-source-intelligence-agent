"""Parse the model's draft response into validated report sections.

The model returns a JSON document; this module is the only place that trusts it, and only
after strict validation. Field names mirror `SaveReportSectionInput` in
`agent/tools/contracts.py`; `tests/agent/test_writing.py` pins that alignment so the two
contracts cannot drift apart silently.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import Field, ValidationError, field_validator

from open_source_intelligence.agent.tools.contracts import (
    MAX_CITATIONS,
    MAX_SECTION_CHARS,
    REPORT_SECTION_KEYS,
    CitationRef,
    StrictModel,
)

_FENCE_PATTERN = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


class DraftPayloadError(ValueError):
    """Raised when a model draft response cannot be validated."""


class DraftSection(StrictModel):
    """One validated draft section produced by the model."""

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


class DraftPayload(StrictModel):
    """The top-level object a model response must contain."""

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


def parse_draft_payload(text: str) -> list[DraftSection]:
    """Validate a model draft response and return one section per template slot."""
    try:
        payload = DraftPayload.model_validate(_extract_json(text))
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
