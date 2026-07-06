from __future__ import annotations

from typing import Any, Mapping

from ...contracts import GraphNodeSpec, ModelRequest, RuntimeSpec, validate_runtime_spec_payload
from ...utils import now_ts, stable_hash
from .state import LangGraphNodeResult, LangGraphRuntimeState


def _digest_safe(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, Mapping):
        return {str(key): _digest_safe(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, list | tuple):
        return [_digest_safe(item) for item in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


class RuntimeOperationService:
    """Shared operation service for spec-backed graph nodes.

    This class is intentionally thin. It centralizes budget/trace/side-effect
    accounting and leaves the existing Agintor kernel behavior available for
    host protocol execution. Generated LangGraph nodes call this service rather
    than reimplementing receipt and trace semantics.
    """

    def __init__(self, runtime_spec: RuntimeSpec, *, provider: Any | None = None, tools: Mapping[str, Any] | None = None) -> None:
        self.runtime_spec = validate_runtime_spec_payload(runtime_spec)
        self.provider = provider
        self.tools = dict(tools or {})

    def run_node(self, state: LangGraphRuntimeState, node: GraphNodeSpec) -> LangGraphNodeResult:
        state.current_node_id = node.node_id
        self._record(state, "langgraph_node_started", node_id=node.node_id, node_type=node.node_type)
        try:
            if node.node_type in {"direct_response", "agent"}:
                output = self._run_agent_like_node(state, node)
            elif node.node_type == "builtin":
                output = self._run_builtin_node(state, node)
            elif node.node_type == "tool":
                output = self._run_tool_node(state, node)
            elif node.node_type == "merge":
                output = self._run_merge_node(state, node)
            elif node.node_type == "verify":
                output = {"verified": True, "checked_node_ids": list(node.input_keys)}
            elif node.node_type in {"service_action", "repo_patch"}:
                output = self._record_side_effect_intent(state, node)
            else:
                raise ValueError(f"unsupported langgraph node_type {node.node_type!r}")
            key = node.output_key or node.node_id
            state.node_results[node.node_id] = output
            state.artifacts[key] = output
            output_digest = stable_hash("langgraph.node_output", node.node_id, _digest_safe(output))
            input_refs = self._input_refs(state, node)
            if node.node_type == "tool":
                self._record(
                    state,
                    "langgraph_tool_action",
                    node_id=node.node_id,
                    node_type=node.node_type,
                    tool_id=str(node.tool_id or ""),
                    args_digest=stable_hash("langgraph.tool_args", _digest_safe(node.static_args)),
                    output_digest=output_digest,
                    status="completed",
                )
            self._record(
                state,
                "langgraph_node_completed",
                node_id=node.node_id,
                node_type=node.node_type,
                output_key=key,
                input_refs=input_refs,
                output_digest=output_digest,
            )
            return LangGraphNodeResult(node_id=node.node_id, output_key=key, output=output, status="completed")
        except Exception as exc:
            state.status = "failed"
            state.error = str(exc)
            self._record(state, "langgraph_node_failed", node_id=node.node_id, node_type=node.node_type, error=str(exc))
            return LangGraphNodeResult(node_id=node.node_id, output_key=node.output_key or node.node_id, output=None, status="failed")

    def _run_agent_like_node(self, state: LangGraphRuntimeState, node: GraphNodeSpec) -> Any:
        agent = next((agent for agent in self.runtime_spec.agents if agent.agent_id == node.agent_id), None)
        prompt = state.prompt
        if agent is not None:
            prompt = agent.prompt.task_template.replace("{prompt}", state.prompt)
        if self.provider is not None and hasattr(self.provider, "generate") and node.node_type == "agent":
            response = self.provider.generate(
                ModelRequest(
                    instructions=agent.prompt.system if agent is not None else "",
                    prompt=prompt,
                    model_class="small",
                    seed=state.seed,
                    metadata={
                        "request_id": state.request_id,
                        "node_id": node.node_id,
                        "runtime_spec_digest": self.runtime_spec.spec_digest,
                        "trace_context": state.trace_context.model_dump(mode="json", exclude_none=True)
                        if state.trace_context is not None
                        else {},
                    },
                )
            )
            return getattr(response, "text", response)
        return {"answer": prompt, "node_id": node.node_id, "runtime_spec_digest": self.runtime_spec.spec_digest}

    def _run_builtin_node(self, state: LangGraphRuntimeState, node: GraphNodeSpec) -> Any:
        args = dict(node.static_args)
        if "numbers" in args:
            numbers = [float(value) for value in args.get("numbers", [])]
            return {"sum": sum(numbers), "product": self._product(numbers)}
        return args or {"ok": True}

    def _run_tool_node(self, state: LangGraphRuntimeState, node: GraphNodeSpec) -> Any:
        visible_tool_ids = {tool.tool_id for tool in self.runtime_spec.tools if tool.runtime_visible}
        if str(node.tool_id) not in visible_tool_ids:
            raise ValueError(f"tool node {node.node_id!r} references non-runtime-visible tool {node.tool_id!r}")
        tool = self.tools.get(str(node.tool_id))
        if callable(tool):
            return tool(**dict(node.static_args))
        return {"tool_id": node.tool_id, "args": dict(node.static_args), "status": "not_bound"}

    @staticmethod
    def _run_merge_node(state: LangGraphRuntimeState, node: GraphNodeSpec) -> Any:
        return {key: state.artifacts.get(key, state.node_results.get(key)) for key in node.input_keys}

    def _record_side_effect_intent(self, state: LangGraphRuntimeState, node: GraphNodeSpec) -> dict[str, Any]:
        payload = {"node_id": node.node_id, "node_type": node.node_type, "static_args": dict(node.static_args)}
        digest = stable_hash(state.request_id, node.node_id, node.node_type, _digest_safe(payload))
        receipt = {
            "side_effect_id": f"langgraph.{node.node_type}.{digest[:12]}",
            "action_fingerprint": digest,
            "idempotency_key": digest,
            "action_kind": "service_action" if node.node_type == "service_action" else "filesystem_write",
            "request_id": state.request_id,
            "node_id": node.node_id,
            "request_digest": digest,
            "backend": "langgraph_spec",
            "status": "launched",
            "outcome_status": "noop",
            "result_ref": {"request": payload},
            "created_at": now_ts(),
        }
        state.side_effect_receipts.append(receipt)
        return {"side_effect_receipt": receipt, "host_execution_required": True}

    @staticmethod
    def _input_refs(state: LangGraphRuntimeState, node: GraphNodeSpec) -> list[dict[str, Any]]:
        refs: list[dict[str, Any]] = []
        for key in node.input_keys:
            value = state.artifacts.get(key, state.node_results.get(key))
            refs.append(
                {
                    "key": str(key),
                    "digest": stable_hash("langgraph.node_input", node.node_id, key, _digest_safe(value)),
                    "present": key in state.artifacts or key in state.node_results,
                }
            )
        return refs

    @staticmethod
    def _product(numbers: list[float]) -> float:
        result = 1.0
        for number in numbers:
            result *= number
        return result

    @staticmethod
    def _record(state: LangGraphRuntimeState, event: str, **payload: Any) -> None:
        state.trace.append({"event": event, "created_at": now_ts(), "request_id": state.request_id, **payload})


__all__ = ["RuntimeOperationService"]
