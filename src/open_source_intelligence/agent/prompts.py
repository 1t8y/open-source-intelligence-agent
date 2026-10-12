"""Versioned prompt assets loaded from the repository `prompts/` directory.

Prompt files follow `prompts/<stage>_<version>.md`. Adding a new prompt version means
adding a file and pointing `AGENT_PROMPT_VERSION` at it, so every Agent run can be traced
back to the exact instructions that produced it.
"""

from __future__ import annotations

from functools import cache
from pathlib import Path
from typing import Literal

PromptStage = Literal["plan", "draft"]

PROMPT_STAGES: tuple[PromptStage, ...] = ("plan", "draft")


def _candidate_bases() -> tuple[Path, ...]:
    """Return every directory that may contain the prompt folder."""
    bases = [Path.cwd()]
    bases.extend(Path(__file__).resolve().parents)
    return tuple(bases)


def resolve_prompts_dir(configured: str | None = None) -> Path:
    """Locate the prompt directory for repository, test, and container layouts.

    An explicit `AGENT_PROMPTS_DIR` wins. Otherwise the working directory and every
    ancestor of this module are probed, which covers both a repository checkout and the
    `/app` working directory used by the container image.
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
    """Return the text of one versioned prompt asset."""
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
    """Clear the prompt cache for tests and environment reloads."""
    load_prompt.cache_clear()
