"""Tests for model draft parsing and validation."""

from __future__ import annotations

import json
from typing import Any

import pytest

from open_source_intelligence.agent.tools.contracts import (
    REPORT_SECTION_KEYS,
    SaveReportSectionInput,
)
from open_source_intelligence.agent.writing import (
    DraftPayloadError,
    DraftSection,
    parse_draft_payload,
)


def _sections(**overrides: Any) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = [
        {"section_key": key, "content_markdown": f"## {key}", "citations": []}
        for key in REPORT_SECTION_KEYS
    ]
    if overrides:
        sections[0].update(overrides)
    return sections


def _payload(**overrides: Any) -> str:
    return json.dumps({"sections": _sections(**overrides)}, ensure_ascii=False)


def test_parses_every_template_section() -> None:
    sections = parse_draft_payload(_payload())
    assert [section.section_key for section in sections] == list(REPORT_SECTION_KEYS)


def test_accepts_fenced_json() -> None:
    text = "```json\n" + _payload() + "\n```"
    assert len(parse_draft_payload(text)) == len(REPORT_SECTION_KEYS)


def test_accepts_surrounding_prose() -> None:
    text = "Here is the draft:\n" + _payload() + "\nI hope this helps."
    assert len(parse_draft_payload(text)) == len(REPORT_SECTION_KEYS)


def test_accepts_sections_in_any_order() -> None:
    sections = _sections()
    sections.reverse()
    text = json.dumps({"sections": sections})
    assert len(parse_draft_payload(text)) == len(REPORT_SECTION_KEYS)


def test_rejects_missing_section() -> None:
    sections = _sections()[:-1]
    with pytest.raises(DraftPayloadError):
        parse_draft_payload(json.dumps({"sections": sections}))


def test_rejects_duplicate_section() -> None:
    sections = _sections()
    sections[-1] = dict(sections[0])
    with pytest.raises(DraftPayloadError):
        parse_draft_payload(json.dumps({"sections": sections}))


def test_rejects_unknown_section_key() -> None:
    with pytest.raises(DraftPayloadError):
        parse_draft_payload(_payload(section_key="executive_summary"))


def test_rejects_empty_section_body() -> None:
    with pytest.raises(DraftPayloadError):
        parse_draft_payload(_payload(content_markdown=""))


def test_rejects_unknown_citation_type() -> None:
    with pytest.raises(DraftPayloadError):
        parse_draft_payload(
            _payload(
                citations=[{"source_type": "blog", "source_ref": "x"}],
            )
        )


def test_rejects_non_json_output() -> None:
    with pytest.raises(DraftPayloadError):
        parse_draft_payload("I could not produce a draft.")


def test_rejects_empty_output() -> None:
    with pytest.raises(DraftPayloadError):
        parse_draft_payload("")


def test_draft_section_matches_the_save_section_contract() -> None:
    draft_fields = set(DraftSection.model_fields)
    save_fields = set(SaveReportSectionInput.model_fields)
    # The runtime computes `content_digest` itself, so it is the single expected difference.
    assert draft_fields == save_fields - {"content_digest"}


def test_draft_section_reuses_the_shared_section_whitelist() -> None:
    with pytest.raises(ValueError):
        DraftSection(section_key="metrics", content_markdown="x")
