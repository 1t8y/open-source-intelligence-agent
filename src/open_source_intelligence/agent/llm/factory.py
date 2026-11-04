"""按应用配置构建选定的模型客户端。"""

from __future__ import annotations

from open_source_intelligence.core.config import Settings

from .base import ModelClient
from .offline import OfflineModelClient
from .openai_compatible import OpenAICompatibleClient


def build_model_client(settings: Settings) -> ModelClient:
    """按 `AGENT_PROVIDER` 指定的名称构建模型客户端。

    `offline` 不需要网络与凭据，且是默认值，因此本地运行与持续集成都保持确定性。
    `openai_compatible` 会调用远程端点，必须同时给出基地址与 API key —— 这一点由
    `Settings` 强制。
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
