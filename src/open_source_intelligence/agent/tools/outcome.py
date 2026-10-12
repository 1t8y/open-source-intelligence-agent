"""Carrier for the data a tool handler returns to the registry."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """Handler output paired with envelope metadata."""

    data: Any
    result_count: int = 0
    truncated: bool = False
