"""关闭实时投递时使用的、确定性的进程内渠道。

默认传输是离线，理由与默认模型供应商相同：本地运行或持续集成作业必须能在没有网络、没有
凭据的情况下走完整个推送路径 —— 守卫、白名单、幂等、重试、审计。回执携带消息的稳定摘要而
不是传输响应，因此调用方仍能证明*本会*发出什么。
"""

from __future__ import annotations

import hashlib

from .base import DeliveryReceipt, OutboundMessage

#: 回执细节中保留的最长摘要。
DIGEST_LENGTH = 16


def message_digest(message: OutboundMessage) -> str:
    """返回一条出站消息的稳定短摘要。"""
    payload = f"{message.subject}\n{message.body}".encode()
    return hashlib.sha256(payload).hexdigest()[:DIGEST_LENGTH]


class OfflineChannel:
    """在进程内记录一次投递，不离开进程。"""

    def __init__(self, channel: str) -> None:
        self._channel = channel

    @property
    def channel(self) -> str:
        """返回本适配器所顶替的渠道。"""
        return self._channel

    def send(self, *, destination: str, message: OutboundMessage) -> DeliveryReceipt:
        """返回携带消息摘要的确定性成功回执。"""
        return DeliveryReceipt(
            ok=True,
            detail=f"offline:{self._channel}:{destination}:{message_digest(message)}",
        )
