"""HTTP Webhook 投递渠道。

请求体是消息的 JSON 形式。配置了签名密钥时，适配器会针对它实际发送的确切字节追加
`X-OSIA-Signature: sha256=<hex>` 头，使接收方既能校验来源，也能校验载荷在传输途中未被
篡改。非 2xx 响应是一条失败回执而不是异常，因此该行保持可重试。
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
    """校验目的地的结构，不执行 DNS 解析。"""
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
    """拒绝解析到回环、私网或其它非公网地址的 Webhook URL。

    `allow_private` 是仅供测试的逃生口（由 `push_webhook_allow_private_targets` 驱动）：
    本地接收器必然位于回环或私网地址上，打开开关后，`local`/`test` 部署就能证明真实投递
    确实可用。该设置在 `local`/`test` 之外一律被拒绝，因此生产环境中的 SSRF 守卫绝不会
    被放宽。
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
    """返回某次请求体的 HMAC-SHA256 签名头值。"""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class WebhookChannel:
    """把一条报告通知投递到 HTTP 端点。"""

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
        """返回渠道标识。"""
        return "webhook"

    def send(self, *, destination: str, message: OutboundMessage) -> DeliveryReceipt:
        """POST 一条 JSON 通知，配置了密钥时对其签名。"""
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
