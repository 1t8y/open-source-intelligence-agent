"""单次 Agent 运行中由运行时注入的授权范围与预算范围。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class ToolContext:
    """模型无法通过工具参数伪造的可信上下文。"""

    agent_run_id: int
    topic_id: int
    authorized_topic_ids: frozenset[int]
    allowed_tools: tuple[str, ...]
    max_tool_calls: int
    tool_call_count: int = 0
    #: 已写入的 `tool_calls` 行 id，按序排列。`registry` 每记录一条就追加进来；
    #: 图再把尾部这些 id 收进它即将写入的步骤，因为执行调用时步骤行还不存在。
    #: 在 `frozen` dataclass 上原地改列表没问题 —— 它只禁止重新绑定属性。
    recorded_call_ids: list[int] = field(default_factory=list)

    def can_access_topic(self, topic_id: int) -> bool:
        """判断本次运行是否可以读取指定主题的资源。"""
        return topic_id in self.authorized_topic_ids
