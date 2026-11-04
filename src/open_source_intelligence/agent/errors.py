"""稳定的 `agent_runs.error_code` 取值与运行复核拒绝异常，及其直白文案。"""

from __future__ import annotations

AGENT_NO_CANDIDATES = "AGENT_NO_CANDIDATES"
AGENT_MODEL_UNAVAILABLE = "AGENT_MODEL_UNAVAILABLE"
AGENT_DRAFT_INVALID = "AGENT_DRAFT_INVALID"
AGENT_BUDGET_EXCEEDED = "AGENT_BUDGET_EXCEEDED"
AGENT_EVIDENCE_INCOMPLETE = "AGENT_EVIDENCE_INCOMPLETE"
AGENT_WRITE_FAILED = "AGENT_WRITE_FAILED"
AGENT_INTERNAL_ERROR = "AGENT_INTERNAL_ERROR"
AGENT_PROMPT_INJECTION = "AGENT_PROMPT_INJECTION"

AGENT_ERROR_CODES = (
    AGENT_NO_CANDIDATES,
    AGENT_MODEL_UNAVAILABLE,
    AGENT_DRAFT_INVALID,
    AGENT_BUDGET_EXCEEDED,
    AGENT_EVIDENCE_INCOMPLETE,
    AGENT_WRITE_FAILED,
    AGENT_INTERNAL_ERROR,
    AGENT_PROMPT_INJECTION,
)

#: 表示「需要人工看一眼」而非「运行崩了」的错误码。
#:
#: 放在错误码旁边而不是 `agent/runtime.py`，是因为运行时（用来决定终态）和
#: 图（用来在致命的上游失败后停下）都需要它，而图无法导入运行时：图正是由运行时
#: 构建出来的。
REVIEW_REQUIRED_CODES = frozenset(
    {
        AGENT_BUDGET_EXCEEDED,
        AGENT_EVIDENCE_INCOMPLETE,
        AGENT_NO_CANDIDATES,
        AGENT_PROMPT_INJECTION,
        AGENT_WRITE_FAILED,
    }
)


class AgentRunReviewError(Exception):
    """运行复核被拒绝时抛出：运行不存在，或其状态无可复核之处。

    两种情况共用一个类，因为界面侧的调用方无论哪种都做同一件事 —— 展示消息、
    页面保持原样。`reason` 让界面用自己的话说明原因：上层翻译的是错误码，而不是
    转发一句服务层的文案。
    """

    code = "AGENT_RUN_REVIEW_REJECTED"

    def __init__(self, message: str, *, reason: str = "invalid") -> None:
        super().__init__(message)
        self.reason = reason


#: 每个错误码一句直白说明，供运行页面使用。与错误码放在一起，新增错误码时
#: 就无法不注意到页面还没有话可说：页面会回落到「未知原因」，读者只能靠猜 ——
#: 而这些句子存在的意义正是终结这种猜测。
AGENT_ERROR_HINTS: dict[str, str] = {
    AGENT_NO_CANDIDATES: "这个主题没有可研究的候选仓库（候选池是空的，或候选都被排除了）。",
    AGENT_MODEL_UNAVAILABLE: "模型端不可达或超时，这一次没有拿到任何产出。",
    AGENT_DRAFT_INVALID: "模型写出的草稿没通过结构校验，没有落成报告。",
    AGENT_BUDGET_EXCEEDED: "撞上了 token / 费用 / 轮次预算，运行被主动截停。",
    AGENT_EVIDENCE_INCOMPLETE: (
        "证据不完整：缺仓库 README 或缺少可比较的历史报告，验证器不放行成稿。"
        "这是最常见的一种 —— 补数据（跑一次采集 / 换一个更长的窗口）后重跑。"
    ),
    AGENT_WRITE_FAILED: "结果落库时写失败，成稿没有保存下来。",
    AGENT_INTERNAL_ERROR: "运行内部异常中断，属于程序问题而非数据问题。",
    AGENT_PROMPT_INJECTION: "抓回的仓库说明里含有指令注入，按隔离策略当场停下，没有继续调用模型。",
}
