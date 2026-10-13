"""Delivery adapters for email and webhook notifications."""

from .base import DeliveryReceipt, NotificationChannel, OutboundMessage
from .email_channel import EmailChannel
from .factory import CHANNELS, build_channel
from .offline import OfflineChannel
from .webhook_channel import SIGNATURE_HEADER, WebhookChannel, sign_body

__all__ = [
    "CHANNELS",
    "DeliveryReceipt",
    "EmailChannel",
    "NotificationChannel",
    "OfflineChannel",
    "OutboundMessage",
    "SIGNATURE_HEADER",
    "WebhookChannel",
    "build_channel",
    "sign_body",
]
