"""按配置与目标渠道构造投递适配器。"""

from __future__ import annotations

from open_source_intelligence.core.config import Settings

from ..errors import PushTargetNotAllowed
from .base import NotificationChannel
from .email_channel import EmailChannel
from .offline import OfflineChannel
from .webhook_channel import WebhookChannel

CHANNELS = ("email", "webhook")


def build_channel(settings: Settings, channel: str) -> NotificationChannel:
    """返回 `channel` 对应的适配器，并遵循已配置的投递模式。

    `offline` 是默认值且无需任何配置，从而让本地运行与持续集成保持确定性且不联网。`live`
    选用真实传输，并把缺失的凭据报告为与 `PUSH_TARGET_NOT_ALLOWED` 相邻的配置错误，
    而不是悄悄假装投递成功。
    """
    if channel not in CHANNELS:
        raise PushTargetNotAllowed(f"unsupported push channel: {channel}")
    if settings.push_delivery == "offline":
        return OfflineChannel(channel)
    if channel == "email":
        if settings.push_smtp_host is None or settings.push_email_from is None:
            raise PushTargetNotAllowed(
                "live email delivery requires push_smtp_host and push_email_from"
            )
        password = (
            settings.push_smtp_password.get_secret_value()
            if settings.push_smtp_password is not None
            else None
        )
        return EmailChannel(
            host=settings.push_smtp_host,
            port=settings.push_smtp_port,
            sender=settings.push_email_from,
            username=settings.push_smtp_username,
            password=password,
            starttls=settings.push_smtp_starttls,
            timeout_seconds=settings.push_timeout_seconds,
        )
    signing_secret = (
        settings.push_webhook_signing_secret.get_secret_value()
        if settings.push_webhook_signing_secret is not None
        else None
    )
    return WebhookChannel(
        signing_secret=signing_secret,
        timeout_seconds=settings.push_timeout_seconds,
        verify=settings.push_webhook_ca_bundle or True,
        allow_private=settings.push_webhook_allow_private_targets,
    )
