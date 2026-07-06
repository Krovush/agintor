from __future__ import annotations

from typing import Any

from ...contracts import ValidatorResult, ValidatorSpec
from ...contracts.evidence import RuntimeEvidenceManifest
from ..validator_registry import ValidatorFamily


def _manifest(payload: dict[str, Any]) -> tuple[RuntimeEvidenceManifest | None, str]:
    raw = payload.get("runtime_evidence_manifest")
    if not isinstance(raw, dict) or not raw:
        return None, "missing_runtime_evidence_manifest"
    try:
        return RuntimeEvidenceManifest.model_validate(raw), ""
    except Exception as exc:
        return None, f"malformed_runtime_evidence_manifest:{exc}"


def _manifest_identity_mismatches(manifest: RuntimeEvidenceManifest, payload: dict[str, Any]) -> list[str]:
    mismatches: list[str] = []
    for field in ("request_id", "task_id", "runtime_hash"):
        value = payload.get(field)
        if value is None or value == "":
            continue
        if str(value) != str(getattr(manifest, field)):
            mismatches.append(field)
    return mismatches


def _event_names(rows: list[Any]) -> set[str]:
    events: set[str] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        event = str(row.get("event", "") or "").strip()
        if event:
            events.add(event)
    return events


def _run(spec: ValidatorSpec, payload: dict[str, Any]) -> ValidatorResult:
    required_events = set(spec.inputs.get("required_events", []))
    forbidden_events = set(spec.inputs.get("forbidden_events", []))
    if not required_events and not forbidden_events:
        return ValidatorResult(
            validator_id=spec.validator_id,
            family_id=spec.family_id,
            claim_ids=list(spec.claim_ids),
            status="abstain",
            authority_used="A0",
            health_status={"trajectory_checks": False},
            observations={"reason": "missing_trace_obligations", "event_count": 0},
        )
    manifest, manifest_reason = _manifest(payload)
    if manifest is None:
        return ValidatorResult(
            validator_id=spec.validator_id,
            family_id=spec.family_id,
            claim_ids=list(spec.claim_ids),
            status="abstain",
            authority_used="A0",
            health_status={"runtime_evidence_manifest": False, "trajectory_checks": False},
            observations={"reason": manifest_reason, "event_count": 0},
        )
    identity_mismatches = _manifest_identity_mismatches(manifest, payload)
    if identity_mismatches:
        return ValidatorResult(
            validator_id=spec.validator_id,
            family_id=spec.family_id,
            claim_ids=list(spec.claim_ids),
            status="abstain",
            authority_used="A0",
            health_status={"runtime_evidence_manifest": True, "manifest_identity": False, "trajectory_checks": False},
            observations={
                "reason": "runtime_evidence_manifest_identity_mismatch",
                "mismatched_fields": identity_mismatches,
                "payload_request_id": str(payload.get("request_id", "") or ""),
                "manifest_request_id": manifest.request_id,
                "payload_task_id": str(payload.get("task_id", "") or ""),
                "manifest_task_id": manifest.task_id,
                "payload_runtime_hash": str(payload.get("runtime_hash", "") or ""),
                "manifest_runtime_hash": manifest.runtime_hash,
                "trace_digest": manifest.trace_digest,
                "manifest_id": manifest.manifest_id,
                "event_count": len(manifest.trace_events),
            },
        )
    trace = [event.model_dump(mode="json", exclude_none=True) for event in manifest.trace_events]
    captured_trace = payload.get("trace")
    captured_events = _event_names(captured_trace if isinstance(captured_trace, list) else [])
    manifest_events = _event_names(trace)
    if captured_events and captured_events != manifest_events:
        return ValidatorResult(
            validator_id=spec.validator_id,
            family_id=spec.family_id,
            claim_ids=list(spec.claim_ids),
            status="abstain",
            authority_used="A0",
            health_status={"runtime_evidence_manifest": True, "manifest_trace_consistency": False, "trajectory_checks": False},
            observations={
                "reason": "runtime_evidence_manifest_trace_divergence",
                "captured_events": sorted(captured_events),
                "manifest_events": sorted(manifest_events),
                "missing_from_manifest": sorted(captured_events - manifest_events),
                "extra_in_manifest": sorted(manifest_events - captured_events),
                "trace_digest": manifest.trace_digest,
                "manifest_id": manifest.manifest_id,
            },
        )
    observed_events = {str(row.get("event")) for row in trace if isinstance(row, dict) and row.get("event") is not None}
    missing = sorted(required_events - observed_events)
    forbidden_seen = sorted(forbidden_events & observed_events)
    trace_present = bool(observed_events)
    passed = trace_present and not missing and not forbidden_seen
    return ValidatorResult(
        validator_id=spec.validator_id,
        family_id=spec.family_id,
        claim_ids=list(spec.claim_ids),
        status="pass" if passed else "fail",
        authority_used="A3" if trace_present else "A0",
        health_status={"runtime_evidence_manifest": True, "trace_present": trace_present, "trajectory_checks": True},
        observations={
            "missing_events": missing,
            "forbidden_events_seen": forbidden_seen,
            "event_count": len(observed_events),
            "reason": "" if trace_present else "missing_trace",
            "trace_digest": manifest.trace_digest,
            "manifest_id": manifest.manifest_id,
        },
    )


def _applicability(context: dict[str, Any]) -> float:
    return 0.7 if context.get("trace_available", True) else 0.0


def family() -> ValidatorFamily:
    return ValidatorFamily(
        family_id="trace_state",
        description="Trajectory/graph-state validator for required and forbidden nodes, tools, budget, and side-effect receipts.",
        authority_ceiling="A3",
        default_visibility="sealed",
        leakage_risk="low",
        default_failure_action="abstain",
        input_contract={"requires": ["trace"], "requires_any": ["required_events", "forbidden_events"]},
        output_schema={"type": "object", "properties": {"missing_events": {"type": "array"}}},
        run=_run,
        applicability=_applicability,
    )
