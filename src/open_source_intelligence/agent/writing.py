"""把模型的草稿响应解析成通过校验的报告章节。

我们要求模型给出 JSON 文档，但真实模型偶尔会无视格式，直接用 Markdown 作答。两种
都接受：先解析 JSON（它带有可追溯性检查所需的引用），当响应里没有 JSON
对象时，再走 Markdown 兜底，按标题切分答案。兜底对应的是「格式不完美
也行」的契约 —— 报告保持简洁易读，某个章节没带引用时会被标记为待人工
复核，而不是让整轮运行炸掉。

字段名对齐 `agent/tools/contracts.py` 里的 `SaveReportSectionInput`；
`tests/agent/test_writing.py` 钉住这条对齐关系，两份契约就无法各自漂移。
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

#: 模型可用来开启章节的中英文标题，映射到规范键名。
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
    """模型草稿响应无法通过校验时抛出。"""


class DraftSection(BaseModel):
    """模型产出的、已通过校验的一个草稿章节。

    故意宽容（`extra="ignore"`）：模型多加一个 `report_meta` 或 `title`
    字段，不足以否掉一份本来能用的草稿。未知字段被丢弃，已知字段被保留，
    于是失败率反映的是内容本身，而不是外层信封。
    """

    model_config = ConfigDict(extra="ignore")

    section_key: str
    content_markdown: str = Field(min_length=1, max_length=MAX_SECTION_CHARS)
    citations: list[CitationRef] = Field(default_factory=list, max_length=MAX_CITATIONS)

    @field_validator("section_key")
    @classmethod
    def validate_section_key(cls, value: str) -> str:
        """只有报告模板里的章节可以被起草。"""
        if value not in REPORT_SECTION_KEYS:
            raise ValueError(f"section_key must be one of: {', '.join(REPORT_SECTION_KEYS)}")
        return value


class DraftPayload(BaseModel):
    """模型响应可能包含的顶层对象。"""

    model_config = ConfigDict(extra="ignore")

    sections: list[DraftSection]


def _json_candidates(text: str) -> tuple[str, ...]:
    """返回响应里可能藏着 JSON 的切片，宽松程度依次递增。"""
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
    """返回从模型响应中解析出的第一个 JSON 值。"""
    for candidate in _json_candidates(text.strip()):
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise DraftPayloadError("model response did not contain a JSON object")


def _split_markdown_sections(text: str) -> list[DraftSection]:
    """把纯 Markdown 的回答按标题切分成章节。

    这是给「无视 JSON 契约、直接写了份正常报告」的模型准备的兜底。每个
    `## heading` 成为一个章节；能映射到已知键名的标题被保留，而没有标题的
    段落会挂到第一个已知章节上。这些章节不带引用，因此会在 `verify` 阶段
    被标记为待复核，而不是带着捏造的证据发布出去。
    """
    stripped = text.strip()
    if not stripped:
        raise DraftPayloadError("model response was empty")

    # 先标出各标题的位置；第一个标题之前的内容属于导语，直接丢掉。
    matches = list(_HEADING_PATTERN.finditer(stripped))
    if not matches:
        # 一个标题都没有：把整篇回答当成单个摘要块。
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
    if not sections:
        # 有标题，但没有一个是报告章节：模型答的是别的东西（一份计划、一句道歉、
        # 一段工具轨迹）。返回空列表会被记为「保存了 0 个章节，内容可
        # 恢复」并送人工复核，而诚实的读法是：根本没有草稿到达。
        raise DraftPayloadError(
            "model response contained headings but no recognised report section"
        )
    return sections


def _dedupe_keep_first(sections: list[DraftSection]) -> list[DraftSection]:
    """每个键只保留第一个章节并保持顺序，重复的标题因此不会重复计入。"""
    seen: set[str] = set()
    ordered: list[DraftSection] = []
    for section in sections:
        if section.section_key in seen:
            continue
        seen.add(section.section_key)
        ordered.append(section)
    return ordered


def parse_draft_payload(text: str) -> list[DraftSection]:
    """校验模型草稿响应，并为每个模板槽位返回一个章节。

    JSON 是主契约，必须每个章节恰好出现一次。若模型改用 Markdown
    作答，则按标题切分；缺失的章节就保持缺失（由调用方判断这是否可
    复核），而不是在这里编造出来。
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
