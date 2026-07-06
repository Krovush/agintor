from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, Field

from ...contracts.evidence import (
    RuntimeArtifactRef,
    RuntimeDeclaredClaim,
    RuntimeEvidenceManifest,
    RuntimeNodeIORef,
    RuntimeSideEffectIntent,
    RuntimeToolAction,
    RuntimeTraceEventRef,
)
from ...contracts import OpenAITraceContext
from ...utils import stable_hash


class LangGraphRuntimeState(BaseModel):
    request_id: str = ""
    task_id: str = ""
    seed: int = 0
    prompt: str = ""
    runtime_hash: str = ""
    runtime_spec_digest: str = ""
    trace_context: OpenAITraceContext | None = None
    current_node_id: str = ""
    artifacts: dict[str, Any] = Field(default_factory=dict)
    node_results: dict[str, Any] = Field(default_factory=dict)
    trace: list[dict[str, Any]] = Field(default_factory=list)
    budget: dict[str, Any] = Field(default_factory=dict)
    side_effect_receipts: list[dict[str, Any]] = Field(default_factory=list)
    runtime_evidence_manifest: dict[str, Any] = Field(default_factory=dict)
    status: Literal["running", "completed", "failed"] = "running"
    error: str = ""


class LangGraphNodeResult(BaseModel):
    node_id: str
    output_key: str = ""
    output: Any = None
    status: Literal["completed", "failed", "skipped"] = "completed"
    trace_rows: list[dict[str, Any]] = Field(default_factory=list)


def _manifest_safe(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, Mapping):
        return {str(key): _manifest_safe(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, list | tuple):
        return [_manifest_safe(item) for item in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)


def _declared_claims_from_artifacts(artifacts: Mapping[str, Any]) -> list[RuntimeDeclaredClaim]:
    claims: list[RuntimeDeclaredClaim] = []
    seen: set[str] = set()
    for artifact in artifacts.values():
        if not isinstance(artifact, Mapping):
            continue
        raw_claims = artifact.get("declared_claims", artifact.get("claims", []))
        if not isinstance(raw_claims, list):
            continue
        for raw_claim in raw_claims:
            if isinstance(raw_claim, Mapping):
                claim_id = str(raw_claim.get("claim_id", "") or "").strip()
                status = str(raw_claim.get("status", "claimed") or "claimed")
                text = str(raw_claim.get("text", "") or "")
            else:
                claim_id = str(raw_claim or "").strip()
                status = "claimed"
                text = ""
            if not claim_id or claim_id in seen:
                continue
            seen.add(claim_id)
            claims.append(
                RuntimeDeclaredClaim(
                    claim_id=claim_id,
                    status=status if status in {"claimed", "abstained", "residual"} else "claimed",
                    text=text,
                )
            )
    return claims


def _artifact_refs(artifacts: Mapping[str, Any]) -> list[RuntimeArtifactRef]:
    refs: list[RuntimeArtifactRef] = []
    for key, value in sorted(artifacts.items(), key=lambda item: str(item[0])):
        clean_key = str(key)
        safe_value = _manifest_safe(value)
        refs.append(
            RuntimeArtifactRef(
                ref_id=f"artifact.{stable_hash(clean_key, safe_value)[:16]}",
                key=clean_key,
                digest=stable_hash("runtime.artifact", clean_key, safe_value),
            )
        )
    return refs


def _trace_event_refs(trace: Sequence[Mapping[str, Any]]) -> list[RuntimeTraceEventRef]:
    refs: list[RuntimeTraceEventRef] = []
    for row in trace:
        event = str(row.get("event", "") or "").strip()
        if not event:
            continue
        metadata = {
            str(key): _manifest_safe(value)
            for key, value in row.items()
            if key not in {"event", "created_at", "request_id", "node_id", "node_type", "output_key"}
        }
        refs.append(
            RuntimeTraceEventRef(
                event=event,
                node_id=str(row.get("node_id", "") or ""),
                node_type=str(row.get("node_type", "") or ""),
                output_key=str(row.get("output_key", "") or ""),
                metadata=metadata,
            )
        )
    return refs


def _node_io_refs(trace: Sequence[Mapping[str, Any]], artifact_ref_ids: Mapping[str, str]) -> list[RuntimeNodeIORef]:
    refs: list[RuntimeNodeIORef] = []
    for row in trace:
        if str(row.get("event", "") or "") != "langgraph_node_completed":
            continue
        node_id = str(row.get("node_id", "") or "")
        output_key = str(row.get("output_key", "") or "")
        refs.append(
            RuntimeNodeIORef(
                node_id=node_id,
                node_type=str(row.get("node_type", "") or ""),
                input_refs=[dict(item) for item in row.get("input_refs", []) if isinstance(item, Mapping)],
                output_ref_id=artifact_ref_ids.get(output_key, "") if output_key else "",
                output_key=output_key,
                output_digest=str(row.get("output_digest", "") or ""),
                status="completed",
            )
        )
    return refs


def _tool_actions(trace: Sequence[Mapping[str, Any]]) -> list[RuntimeToolAction]:
    actions: list[RuntimeToolAction] = []
    for row in trace:
        if str(row.get("event", "") or "") != "langgraph_tool_action":
            continue
        actions.append(
            RuntimeToolAction(
                node_id=str(row.get("node_id", "") or ""),
                tool_id=str(row.get("tool_id", "") or ""),
                args_digest=str(row.get("args_digest", "") or ""),
                output_digest=str(row.get("output_digest", "") or ""),
                status=str(row.get("status", "") or ""),
            )
        )
    return actions


def _side_effect_intents(receipts: Sequence[Mapping[str, Any]]) -> list[RuntimeSideEffectIntent]:
    intents: list[RuntimeSideEffectIntent] = []
    for receipt in receipts:
        request = {}
        result_ref = receipt.get("result_ref", {})
        if isinstance(result_ref, Mapping):
            request = dict(result_ref.get("request", {}) or {})
        node_type = str(request.get("node_type", "") or "")
        action_kind = str(receipt.get("action_kind", "") or "")
        if node_type in {"repo_patch", "service_action"}:
            intent_kind = node_type
        elif action_kind == "service_action":
            intent_kind = "service_action"
        elif action_kind == "filesystem_write":
            intent_kind = "filesystem_write"
        else:
            intent_kind = "unknown"
        intents.append(
            RuntimeSideEffectIntent(
                node_id=str(receipt.get("node_id", "") or request.get("node_id", "") or ""),
                intent_kind=intent_kind,
                args_digest=str(receipt.get("request_digest", "") or stable_hash(request)),
                receipt_ids=[str(receipt.get("side_effect_id", "") or "")],
                status=str(receipt.get("status", "") or ""),
                metadata={"action_kind": action_kind, "outcome_status": str(receipt.get("outcome_status", "") or "")},
            )
        )
    return intents


def build_runtime_evidence_manifest(
    state: LangGraphRuntimeState,
    *,
    declared_claim_ids: Sequence[str] = (),
) -> RuntimeEvidenceManifest:
    claims = _declared_claims_from_artifacts(state.artifacts)
    seen_claims = {claim.claim_id for claim in claims}
    for claim_id in declared_claim_ids:
        clean = str(claim_id or "").strip()
        if clean and clean not in seen_claims:
            seen_claims.add(clean)
            claims.append(RuntimeDeclaredClaim(claim_id=clean, source="task_contract"))
    if state.status == "failed" and state.error:
        claims.append(RuntimeDeclaredClaim(claim_id="runtime.execution", status="abstained", text=state.error))
    trace_rows = [dict(row) for row in state.trace]
    artifact_refs = _artifact_refs(state.artifacts)
    artifact_ref_ids = {ref.key: ref.ref_id for ref in artifact_refs}
    manifest = RuntimeEvidenceManifest(
        request_id=state.request_id,
        task_id=state.task_id,
        runtime_hash=state.runtime_hash,
        runtime_spec_digest=state.runtime_spec_digest,
        declared_claims=claims,
        artifact_refs=artifact_refs,
        node_io_refs=_node_io_refs(trace_rows, artifact_ref_ids),
        trace_events=_trace_event_refs(trace_rows),
        trace_digest=stable_hash("runtime.trace", _manifest_safe(trace_rows)),
        tool_actions=_tool_actions(trace_rows),
        side_effect_receipts=[dict(receipt) for receipt in state.side_effect_receipts],
        side_effect_intents=_side_effect_intents(state.side_effect_receipts),
        abstentions=[
            {"claim_id": claim.claim_id, "reason": claim.text}
            for claim in claims
            if claim.status == "abstained"
        ],
        residuals=[
            {"claim_id": claim.claim_id, "reason": claim.text}
            for claim in claims
            if claim.status == "residual"
        ],
    )
    state.runtime_evidence_manifest = manifest.model_dump(mode="json", exclude_none=True)
    return manifest


__all__ = ["LangGraphNodeResult", "LangGraphRuntimeState", "build_runtime_evidence_manifest"]
