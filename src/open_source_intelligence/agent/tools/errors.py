"""映射到稳定契约错误码的工具执行异常。"""

from __future__ import annotations

from .contracts import RETRYABLE_ERROR_CODES


class ToolExecutionError(Exception):
    """由工具处理器抛出，用来返回指定的契约错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    @property
    def retryable(self) -> bool:
        """调用方是否可以重试同一次调用。"""
        return self.code in RETRYABLE_ERROR_CODES
