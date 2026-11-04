"""承载工具处理器返回给 `registry` 的数据。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ToolOutcome:
    """处理器的输出，连同信封元数据一起返回。"""

    data: Any
    result_count: int = 0
    truncated: bool = False
