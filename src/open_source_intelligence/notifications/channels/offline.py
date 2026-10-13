"""Deterministic in-process channel used when live delivery is switched off.

The default transport is offline for the same reason the default model provider is: a local run
or a continuous-integration job must be able to exercise the whole push path — guards, the
allowlist, idempotency, retries, audit — without a network or a credential. The receipt carries
a stable digest of the message instead of a transport response, so a caller can still prove
*what* would have been sent.
"""

from __future__ import annotations

import hashlib

from .base import DeliveryReceipt, OutboundMessage

#: Longest digest kept in a receipt detail.
DIGEST_LENGTH = 16


def message_digest(message: OutboundMessage) -> str:
    """Return a stable short digest of one outbound message."""
    payload = f"{message.subject}\n{message.body}".encode()
    return hashlib.sha256(payload).hexdigest()[:DIGEST_LENGTH]


class OfflineChannel:
    """Record a delivery without leaving the process."""

    def __init__(self, channel: str) -> None:
        self._channel = channel

    @property
    def channel(self) -> str:
        """Return the channel this adapter stands in for."""
        return self._channel

    def send(self, *, destination: str, message: OutboundMessage) -> DeliveryReceipt:
        """Return a deterministic success receipt carrying the message digest."""
        return DeliveryReceipt(
            ok=True,
            detail=f"offline:{self._channel}:{destination}:{message_digest(message)}",
        )
