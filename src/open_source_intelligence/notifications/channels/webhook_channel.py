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
import socket
from ipaddress import ip_address
from urllib.parse import SplitResult, urlsplit

import httpx

from ..errors import PushTargetNotAllowed
from .base import MAX_DETAIL_LENGTH, DeliveryReceipt, OutboundMessage

SIGNATURE_HEADER = "X-OSIA-Signature"


def validate_webhook_url(destination: str) -> SplitResult:
    """Validate the destination shape without performing DNS resolution."""
    parts = urlsplit(destination)
    if parts.scheme != "https" or not parts.hostname:
        raise PushTargetNotAllowed("webhook destinations must be absolute HTTPS URLs")
    if parts.username is not None or parts.password is not None:
        raise PushTargetNotAllowed("webhook destinations must not contain credentials")
    try:
        _ = parts.port
    except ValueError as exc:
        raise PushTargetNotAllowed("webhook destination contains an invalid port") from exc
    return parts


def validate_webhook_destination(
    destination: str,
    *,
    allow_private: bool = False,
) -> None:
    """Reject webhook URLs that resolve to loopback, private, or other non-public addresses.

    `allow_private` is the test-only escape hatch (driven by
    `push_webhook_allow_private_targets`): a local receiver necessarily lives on a loopback or
    private address, and flipping it lets a `local`/`test` deployment prove real delivery. The
    setting is refused outside `local`/`test`, so the SSRF guard is never widened in production.
    """
    parts = validate_webhook_url(destination)
    hostname = parts.hostname
    assert hostname is not None
    try:
        addresses = {
            info[4][0]
            for info in socket.getaddrinfo(
                hostname,
                parts.port or 443,
                type=socket.SOCK_STREAM,
            )
        }
    except socket.gaierror as exc:
        raise PushTargetNotAllowed(f"webhook host cannot be resolved: {hostname}") from exc
    if not addresses:
        raise PushTargetNotAllowed(f"webhook host cannot be resolved: {hostname}")
    if allow_private:
        return
    for address in addresses:
        if not ip_address(address).is_global:
            raise PushTargetNotAllowed(
                f"webhook destination resolves to a non-public address: {hostname}"
            )


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
        verify: bool | str = True,
        allow_private: bool = False,
    ) -> None:
        self._signing_secret = signing_secret
        self._timeout = timeout_seconds
        self._verify = verify
        self._allow_private = allow_private

    @property
    def channel(self) -> str:
        """Return the channel identifier."""
        return "webhook"

    def send(self, *, destination: str, message: OutboundMessage) -> DeliveryReceipt:
        """POST one JSON notification, signing it when a secret is configured."""
        validate_webhook_destination(destination, allow_private=self._allow_private)
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
                follow_redirects=False,
                trust_env=False,
                verify=self._verify,
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
