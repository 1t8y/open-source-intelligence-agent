"""定义模型供应商适配器抛出的稳定错误码。"""

from __future__ import annotations

from decimal import Decimal

MODEL_REQUEST_FAILED = "MODEL_REQUEST_FAILED"
MODEL_TIMEOUT = "MODEL_TIMEOUT"
MODEL_RATE_LIMITED = "MODEL_RATE_LIMITED"
MODEL_INVALID_RESPONSE = "MODEL_INVALID_RESPONSE"

MODEL_ERROR_CODES = (
    MODEL_REQUEST_FAILED,
    MODEL_TIMEOUT,
    MODEL_RATE_LIMITED,
    MODEL_INVALID_RESPONSE,
)

RETRYABLE_MODEL_ERRORS = frozenset({MODEL_TIMEOUT, MODEL_RATE_LIMITED})


class ModelClientError(Exception):
    """供应商故障，携带稳定错误码，而不是供应商自己那句提示文本。

    `prompt_tokens`、`completion_tokens` 与 `cost` 记录的是供应商已经
    计过费的用量：请求在响应到达*之后*才失败（答案被截断、响应体无法
    解析）。既然付过费，这次调用就一定要进账，所以图把这些用量累加进
    本次运行的总额，而不是让预算悄悄往回走。
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        cost: Decimal = Decimal("0"),
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.prompt_tokens = max(0, prompt_tokens)
        self.completion_tokens = max(0, completion_tokens)
        self.cost = cost

    @property
    def retryable(self) -> bool:
        """返回调用方是否可以安全地重试同一个请求。"""
        return self.code in RETRYABLE_MODEL_ERRORS
