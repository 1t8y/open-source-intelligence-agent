"""Expose the tool whitelist to the model as JSON schemas."""

from __future__ import annotations

from open_source_intelligence.agent.llm.messages import ToolSchema
from open_source_intelligence.agent.tools.registry import ToolRegistry


def build_tool_schemas(registry: ToolRegistry) -> tuple[ToolSchema, ...]:
    """Return one JSON-schema description per whitelisted tool.

    The schema comes from the same Pydantic model the registry validates against, so the
    model is offered exactly the contract that will later be enforced.
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
