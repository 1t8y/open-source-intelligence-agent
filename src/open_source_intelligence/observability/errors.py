"""把可观测性失败以 Problem Details 形式暴露给 API。

总览本身不可能违反任何业务规则 —— 它只读别的层写下的东西。唯一会出问题的是索要一次并不
存在的运行，所以这条继承体系保持精简，并复用 API 其余部分已经在发的错误码。
"""

from __future__ import annotations


class ObservabilityError(Exception):
    """总览与运行台账失败的基类。"""

    code: str = "OBSERVABILITY_ERROR"


class AgentRunNotFound(ObservabilityError):
    """当被请求的 Agent 运行或它的轨迹不存在时抛出。"""

    code = "NOT_FOUND"


__all__ = ["AgentRunNotFound", "ObservabilityError"]
