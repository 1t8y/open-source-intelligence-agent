"""执行画像：评测运行器拨动的消融开关。

`docs/evaluation.md` §8 要求消融实验回答「去掉某项能力会怎样」。这个答案要
有意义，前提是让*同一段*代码路径在关掉一项能力后运行。所以消融不是重新实现：
它是一份由真实研究运行时读取的画像，而生产运行永远使用 `AGENT_FULL`。

两个开关覆盖了文档里的全部三种消融：

* `run_verifier=False` 去掉证据覆盖闸（`agent_no_verifier`）。
* `disabled_tools` 从运行可调用的白名单里移除读取类工具（`agent_no_tools`
  去掉所有读取工具，`agent_no_history` 只去掉 `search_prior_reports`）。

起草工具被特意设为*不可*禁用：一次完全产不出草稿的消融什么都衡量不了，所以
只要 Agent 配置启用了它，运行时就会一直保留。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from open_source_intelligence.agent.tools.registry import WHITELIST

#: 唯一会写库的工具；每份画像都保留它，消融才仍能产出草稿。
WRITE_TOOL: Final[str] = "save_report_section"

#: 白名单里的所有只读工具，按白名单顺序排列。
READ_TOOLS: Final[tuple[str, ...]] = tuple(name for name in WHITELIST if name != WRITE_TOOL)


@dataclass(frozen=True, slots=True)
class ExecutionProfile:
    """一次研究运行被允许使用的能力集合。"""

    name: str
    run_verifier: bool = True
    disabled_tools: frozenset[str] = field(default_factory=frozenset)

    def allows(self, tool_name: str) -> bool:
        """判断本画像是否允许运行调用某个工具。"""
        return tool_name not in self.disabled_tools


#: 生产画像：不关掉任何东西。
AGENT_FULL: Final[ExecutionProfile] = ExecutionProfile(name="agent_full")

#: 无验证器：平时拦住写作的那道覆盖闸被移除。
PROFILE_NO_VERIFIER: Final[ExecutionProfile] = ExecutionProfile(
    name="agent_no_verifier",
    run_verifier=False,
)

#: 无读取工具：草稿只凭候选上下文写成。
PROFILE_NO_TOOLS: Final[ExecutionProfile] = ExecutionProfile(
    name="agent_no_tools",
    disabled_tools=frozenset(READ_TOOLS),
)

#: 无历史：运行保留其余工具，但不得查询历史报告。
PROFILE_NO_HISTORY: Final[ExecutionProfile] = ExecutionProfile(
    name="agent_no_history",
    disabled_tools=frozenset({"search_prior_reports"}),
)

#: 三个消融目标所用的画像，以各自的 `target_type` 取值作键。
ABLATION_PROFILES: Final[dict[str, ExecutionProfile]] = {
    PROFILE_NO_VERIFIER.name: PROFILE_NO_VERIFIER,
    PROFILE_NO_TOOLS.name: PROFILE_NO_TOOLS,
    PROFILE_NO_HISTORY.name: PROFILE_NO_HISTORY,
}


def profile_for_target(target_type: str) -> ExecutionProfile | None:
    """返回某个 Agent 目标类型所用的画像；不是 Agent 目标时返回 `None`。"""
    if target_type == AGENT_FULL.name:
        return AGENT_FULL
    return ABLATION_PROFILES.get(target_type)
