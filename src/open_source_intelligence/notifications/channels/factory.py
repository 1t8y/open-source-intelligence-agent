"""Build the delivery adapter named by settings and the target's channel."""

from __future__ import annotations

from open_source_intelligence.core.config import Settings

from ..errors import PushTargetNotAllowed
from .base import NotificationChannel
from .email_channel import EmailChannel
from .offline import OfflineChannel
from .webhook_channel import WebhookChannel

CHANNELS = ("email", "webhook")


def build_channel(settings: Settings, channel: str) -> NotificationChannel:
    """Return the adapter for `channel`, honouring the configured delivery mode.

    `offline` is the default and needs no configuration, which keeps local runs and continuous
    integration deterministic and network-free. `live` selects the real transport and reports a
    missing credential as `PUSH_TARGET_NOT_ALLOWED`-adjacent configuration error rather than
    silently pretending to deliver.
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
    )
