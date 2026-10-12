"""Model provider adapters behind one provider-neutral contract."""

from .base import ModelClient
from .errors import MODEL_ERROR_CODES, ModelClientError
from .factory import build_model_client
from .messages import ChatMessage, ModelToolCall, ModelTurn, ToolSchema
from .offline import OfflineModelClient
from .openai_compatible import OpenAICompatibleClient

__all__ = [
    "MODEL_ERROR_CODES",
    "ChatMessage",
    "ModelClient",
    "ModelClientError",
    "ModelToolCall",
    "ModelTurn",
    "OfflineModelClient",
    "OpenAICompatibleClient",
    "ToolSchema",
    "build_model_client",
]
