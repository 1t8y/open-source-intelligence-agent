"""把工具白名单以 JSON Schema 的形式暴露给模型。"""

from __future__ import annotations

from open_source_intelligence.agent.llm.messages import ToolSchema
from open_source_intelligence.agent.tools.registry import ToolRegistry


def build_tool_schemas(registry: ToolRegistry) -> tuple[ToolSchema, ...]:
    """为每个白名单工具返回一份 JSON Schema 描述。

    Schema 取自注册表实际校验所用的那个 Pydantic 模型，因此模型拿到的正是
    之后会被强制执行的那份契约。
    """
    schemas: list[ToolSchema] = []
    for name in registry.tool_names:
        spec = registry.get(name)
        if spec is None:
            continue
        schemas.append(
            ToolSchema(
                name=spec.name,
                description=spec.description,
                parameters=spec.input_model.model_json_schema(),
            )
        )
    return tuple(schemas)
