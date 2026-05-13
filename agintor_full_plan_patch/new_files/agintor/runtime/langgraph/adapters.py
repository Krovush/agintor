from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from ...contracts import ModelPolicy, ToolSpec


@dataclass(frozen=True)
class LangChainToolBinding:
    tool_id: str
    name: str
    category: str
    callable_ref: Callable[..., Any] | None = None
    metadata: dict[str, Any] | None = None


def tool_spec_to_binding(tool: ToolSpec) -> LangChainToolBinding:
    return LangChainToolBinding(
        tool_id=tool.tool_id,
        name=tool.name,
        category=tool.category,
        callable_ref=None,
        metadata={"binding": dict(tool.binding), "side_effect_kind": tool.side_effect_kind},
    )


def model_policy_to_langchain_config(policy: ModelPolicy) -> dict[str, Any]:
    return {
        "provider_name": policy.provider_name,
        "model_class": policy.model_class,
        "temperature": policy.temperature,
        "max_output_tokens": policy.max_output_tokens,
        "metadata": dict(policy.metadata),
    }


__all__ = ["LangChainToolBinding", "model_policy_to_langchain_config", "tool_spec_to_binding"]
