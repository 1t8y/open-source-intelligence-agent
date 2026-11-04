"""与供应商无关的模型客户端契约：每个供应商适配器都必须实现的同步聊天接口。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol, runtime_checkable

from .messages import ChatMessage, ModelTurn, ToolSchema


@runtime_checkable
class ModelClient(Protocol):
    """每个供应商适配器都实现的最小同步聊天契约。"""

    @property
    def provider(self) -> str:
        """返回记录在 Agent 配置上的供应商标识。"""
        ...

    @property
    def model(self) -> str:
        """返回用于计费与审计的具体模型标识。"""
        ...

    def complete(
        self,
        messages: Sequence[ChatMessage],
        tools: Sequence[ToolSchema],
    ) -> ModelTurn:
        """返回给定对话与工具集下的一轮助手回复。"""
        ...
