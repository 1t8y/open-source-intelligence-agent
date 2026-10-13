"""HTTP webhook delivery channel.

The body is the JSON form of the message. When a signing secret is configured the adapter adds
`X-OSIA-Signature: sha256=<hex>` over the exact bytes it sends, so the receiver can verify both
the origin and that the payload was not altered in flight. A non-2xx response is a failed
receipt, not an exception, so the row stays retryable.
"""

from __future__ import annotations

import hashlib
import hmac
import json

import httpx

from .base import MAX_DETAIL_LENGTH, DeliveryReceipt, OutboundMessage

SIGNATURE_HEADER = "X-OSIA-Signature"


def sign_body(secret: str, body: bytes) -> str:
    """Return the HMAC-SHA256 signature header value for one request body."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class WebhookChannel:
    """Deliver a report notification to an HTTP endpoint."""

    def __init__(
        self,
        *,
        signing_secret: str | None = None,
        timeout_seconds: int = 20,
    ) -> None:
        self._signing_secret = signing_secret
        self._timeout = timeout_seconds

    @property
    def channel(self) -> str:
        """Return the channel identifier."""
        return "webhook"

    def send(self, *, destination: str, message: OutboundMessage) -> DeliveryReceipt:
        """POST one JSON notification, signing it when a secret is configured."""
        body = json.dumps(message.to_dict(), ensure_ascii=False, sort_keys=True).encode()
        headers = {"Content-Type": "application/json"}
        if self._signing_secret is not None:
            headers[SIGNATURE_HEADER] = sign_body(self._signing_secret, body)
        try:
            response = httpx.post(
                destination,
                content=body,
                headers=headers,
                timeout=self._timeout,
            )
        except httpx.HTTPError as exc:
            return DeliveryReceipt(
                ok=False,
                detail=f"http:{exc.__class__.__name__}:{exc}"[:MAX_DETAIL_LENGTH],
            )
        if response.status_code >= 400:
            return DeliveryReceipt(
                ok=False,
                detail=f"http:{response.status_code}:{response.text}"[:MAX_DETAIL_LENGTH],
            )
        return DeliveryReceipt(ok=True, detail=f"http:{response.status_code}")
