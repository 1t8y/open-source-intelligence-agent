"""Stable `agent_runs.error_code` values, the run-review refusal, and their plain sentences."""

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

#: Error codes that mean "a human should look at this" rather than "the run broke".
#:
#: This lives next to the codes rather than in `agent/runtime.py` because both the runtime (to
#: decide the terminal status) and the graph (to stop after a fatal upstream failure) need it,
#: and the graph cannot import the runtime: the runtime is what builds the graph.
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
    """Raised when a run review is refused: the run is gone, or its status has nothing to review.

    One class for both because the caller in the interface does the same thing either way - it
    shows the message and leaves the page as it was. `reason` is what lets the interface say why
    in its own words: the layers above translate a code, they do not forward a service sentence.
    """

    code = "AGENT_RUN_REVIEW_REJECTED"

    def __init__(self, message: str, *, reason: str = "invalid") -> None:
        super().__init__(message)
        self.reason = reason


#: One plain sentence per code, for the run page. Kept next to the codes so a new code cannot be
#: added without someone noticing that the page has nothing to say about it: the page falls back to
#: "未知原因" and the reader is left guessing, which is exactly what these sentences exist to stop.
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
