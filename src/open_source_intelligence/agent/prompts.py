"""从仓库的 `prompts/` 目录加载带版本的 Prompt 资产。

Prompt 文件遵循 `prompts/<stage>_<version>.md` 命名。新增一个 Prompt 版本
意味着加一个文件并把 `AGENT_PROMPT_VERSION` 指向它，这样每次 Agent 运行都能
追溯到产出它的那份确切指令。
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Literal

PromptStage = Literal["plan", "draft"]

PROMPT_STAGES: tuple[PromptStage, ...] = ("plan", "draft")


def _candidate_bases() -> tuple[Path, ...]:
    """返回所有可能包含 Prompt 目录的目录。"""
    bases = [Path.cwd()]
    bases.extend(Path(__file__).resolve().parents)
    return tuple(bases)


def resolve_prompts_dir(configured: str | None = None) -> Path:
    """为仓库、测试与容器三种布局定位 Prompt 目录。

    显式给出 `AGENT_PROMPTS_DIR` 时以它为准。否则会探测工作目录以及本模块
    的每一级祖先目录，这样既覆盖仓库检出，也覆盖容器镜像使用的 `/app`
    工作目录。
    """
    if configured:
        candidate = Path(configured).expanduser()
        if candidate.is_dir():
            return candidate.resolve()
        raise FileNotFoundError(f"prompt directory does not exist: {configured}")
    for base in _candidate_bases():
        candidate = base / "prompts"
        if candidate.is_dir():
            return candidate.resolve()
    raise FileNotFoundError("prompt directory 'prompts' was not found")


@cache
def load_prompt(stage: str, version: str, directory: str | None = None) -> str:
    """返回某一版本 Prompt 资产的文本。"""
    if stage not in PROMPT_STAGES:
        raise ValueError(f"unknown prompt stage: {stage}")
    path = resolve_prompts_dir(directory) / f"{stage}_{version}.md"
    if not path.is_file():
        raise FileNotFoundError(f"prompt asset not found: {path}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"prompt asset is empty: {path}")
    return text


def clear_prompt_cache() -> None:
    """清空 Prompt 缓存，供测试和环境变量重载使用。"""
    load_prompt.cache_clear()
