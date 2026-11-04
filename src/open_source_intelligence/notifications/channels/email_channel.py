"""SMTP 投递渠道。

适配器刻意保持很薄：它构造一条纯文本消息交给 `smtplib`，并把 SMTP 会话可能抛出的传输异常
转成失败回执。调用方永远看不到套接字错误，只会看到一条可重试的推送行。
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

from .base import MAX_DETAIL_LENGTH, DeliveryReceipt, OutboundMessage

SMTP_TIMEOUT_SECONDS = 20

#: 值得转成可重试回执、而不是让程序崩溃的传输失败。
SMTP_FAILURES = (
    smtplib.SMTPException,
    OSError,
    TimeoutError,
)


class EmailChannel:
    """通过 SMTP 投递一条报告通知。"""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        sender: str,
        username: str | None = None,
        password: str | None = None,
        starttls: bool = True,
        timeout_seconds: int = SMTP_TIMEOUT_SECONDS,
    ) -> None:
        self._host = host
        self._port = port
        self._sender = sender
        self._username = username
        self._password = password
        self._starttls = starttls
        self._timeout = timeout_seconds

    @property
    def channel(self) -> str:
        """返回渠道标识。"""
        return "email"

    def send(self, *, destination: str, message: OutboundMessage) -> DeliveryReceipt:
        """发送一条纯文本通知，把传输失败报告为回执。"""
        outgoing = EmailMessage()
        outgoing["From"] = self._sender
        outgoing["To"] = destination
        outgoing["Subject"] = message.subject
        outgoing.set_content(message.body)
        try:
            with smtplib.SMTP(self._host, self._port, timeout=self._timeout) as client:
                if self._starttls:
                    client.starttls()
                if self._username is not None:
                    client.login(self._username, self._password or "")
                client.send_message(outgoing)
        except SMTP_FAILURES as exc:
            return DeliveryReceipt(
                ok=False,
                detail=f"smtp:{exc.__class__.__name__}:{exc}"[:MAX_DETAIL_LENGTH],
            )
        return DeliveryReceipt(ok=True, detail=f"smtp:{self._host}:{self._port}")
