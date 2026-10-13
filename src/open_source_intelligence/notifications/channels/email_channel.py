"""SMTP delivery channel.

The adapter is intentionally thin: it builds a plain-text message and hands it to `smtplib`,
turning the transport exceptions an SMTP conversation can raise into a failed receipt. Callers
never see a socket error; they see a push row that can be retried.
"""

from __future__ import annotations

import smtplib
from email.message import EmailMessage

from .base import MAX_DETAIL_LENGTH, DeliveryReceipt, OutboundMessage

SMTP_TIMEOUT_SECONDS = 20

#: Transport failures worth turning into a retryable receipt rather than a crash.
SMTP_FAILURES = (
    smtplib.SMTPException,
    OSError,
    TimeoutError,
)


class EmailChannel:
    """Deliver a report notification over SMTP."""

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
        """Return the channel identifier."""
        return "email"

    def send(self, *, destination: str, message: OutboundMessage) -> DeliveryReceipt:
        """Send one plain-text notification, reporting transport failures as a receipt."""
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
