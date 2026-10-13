"""Channel-neutral delivery contract.

Every adapter implements the same tiny surface: take a destination and an `OutboundMessage`,
return a `DeliveryReceipt`. Nothing above this layer knows how a message travels, which is what
lets the offline adapter stand in for SMTP and HTTP during local runs and continuous
integration without any calling code changing.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

#: Longest transport detail kept on a push row; receipts are diagnostic, not archival.
MAX_DETAIL_LENGTH = 512


@dataclass(frozen=True, slots=True)
class OutboundMessage:
    """One report notification ready to be delivered."""

    report_id: int
    version: int
    subject: str
    body: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe payload for the push record and the webhook body."""
        return {
            "report_id": self.report_id,
            "version": self.version,
            "subject": self.subject,
            "body": self.body,
        }


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    """Outcome of one delivery attempt.

    `ok=False` is a normal, recorded result rather than an exception: it is what turns a push
    row into a retry candidate.
    """

    ok: bool
    detail: str | None = None


@runtime_checkable
class NotificationChannel(Protocol):
    """Delivery adapter for one transport."""

    @property
    def channel(self) -> str:
        """Return the channel identifier recorded on the target and the push row."""
        ...

    def send(self, *, destination: str, message: OutboundMessage) -> DeliveryReceipt:
        """Deliver one message, returning a receipt instead of raising on transport failure."""
        ...
