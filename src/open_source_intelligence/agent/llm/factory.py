"""Build the model client selected by application settings."""

from __future__ import annotations

from open_source_intelligence.core.config import Settings

from .base import ModelClient
from .offline import OfflineModelClient
from .openai_compatible import OpenAICompatibleClient


def build_model_client(settings: Settings) -> ModelClient:
    """Build the model client named by `AGENT_PROVIDER`.

    `offline` needs no network or credential and is the default, which keeps local runs
    and continuous integration deterministic. `openai_compatible` calls a remote endpoint
    and requires both a base URL and an API key, enforced by `Settings`.
    """
    if settings.agent_provider == "openai_compatible":
        if settings.agent_api_base is None or settings.agent_api_key is None:
            raise ValueError(
                "agent_api_base and agent_api_key are required for the openai_compatible provider"
            )
        return OpenAICompatibleClient(
            api_base=settings.agent_api_base,
            api_key=settings.agent_api_key.get_secret_value(),
            model=settings.agent_model,
            timeout_seconds=settings.agent_request_timeout_seconds,
            input_price_per_mtok=settings.agent_input_price_per_mtok,
            output_price_per_mtok=settings.agent_output_price_per_mtok,
        )
    return OfflineModelClient(settings.agent_model)
