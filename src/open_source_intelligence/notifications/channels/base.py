"""与渠道无关的投递契约。

每个适配器都实现同一套极小接口：接收一个目的地与一个 `OutboundMessage`，返回一个
`DeliveryReceipt`。这一层之上没有任何代码知道消息如何传输，正因如此，离线适配器才能在
本地运行与持续集成中顶替 SMTP 与 HTTP，而调用方无需改动任何代码。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

#: 推送行上保留的最长传输细节；回执用于诊断，不用于归档。
MAX_DETAIL_LENGTH = 512


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    """一条待投递的报告通知。"""

    report_id: int
    version: int
    subject: str
    body: str

    def to_dict(self) -> dict[str, Any]:
        """返回供推送记录与 Webhook 请求体使用的 JSON 安全载荷。"""
        return {
            "report_id": self.report_id,
            "version": self.version,
            "subject": self.subject,
            "body": self.body,
        }


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    """一次投递尝试的结果。

    `ok=False` 是正常记录下来的结果，而不是异常：正是它把一条推送行变成重试候选。
    """

    ok: bool
    detail: str | None = None


@runtime_checkable
class NotificationChannel(Protocol):
    """针对单一传输方式的投递适配器。"""

    @property
    def channel(self) -> str:
        """返回记录在目标与推送行上的渠道标识。"""
        ...

    def send(self, *, destination: str, message: OutboundMessage) -> DeliveryReceipt:
        """投递一条消息，传输失败时返回回执而不是抛异常。"""
        ...
