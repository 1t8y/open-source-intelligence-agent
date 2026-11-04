"""守护按版本加载 Prompt 资产的用例。"""

from __future__ import annotations

from pathlib import Path

import pytest

from open_source_intelligence.agent.prompts import (
    PROMPT_STAGES,
    clear_prompt_cache,
    load_prompt,
    resolve_prompts_dir,
)


def test_prompt_directory_is_resolvable() -> None:
    directory = resolve_prompts_dir()
    assert (directory / "plan_v1.md").is_file()
    assert (directory / "draft_v1.md").is_file()


@pytest.mark.parametrize("stage", PROMPT_STAGES)
def test_every_stage_loads_with_untrusted_text_rules(stage: str) -> None:
    clear_prompt_cache()
    text = load_prompt(stage, "v1")
    assert text.strip()
    assert "不可信" in text


def test_unknown_stage_is_rejected() -> None:
    with pytest.raises(ValueError):
        load_prompt("nope", "v1")


def test_missing_version_is_reported() -> None:
    with pytest.raises(FileNotFoundError):
        load_prompt("plan", "v99")


def test_missing_configured_directory_is_reported(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        resolve_prompts_dir(str(tmp_path / "absent"))


def test_explicit_directory_overrides_the_default(tmp_path: Path) -> None:
    target = tmp_path / "prompts"
    target.mkdir()
    (target / "plan_v1.md").write_text("# custom plan", encoding="utf-8")
    clear_prompt_cache()
    assert load_prompt("plan", "v1", str(target)) == "# custom plan"
