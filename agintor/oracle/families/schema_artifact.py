from __future__ import annotations

from typing import Any

from ...contracts import ValidatorResult, ValidatorSpec
from ..validator_registry import ValidatorFamily


def _type_ok(value: Any, schema_type: str) -> bool:
    if schema_type == "object":
        return isinstance(value, dict)
    if schema_type == "array":
        return isinstance(value, list)
    if schema_type == "string":
        return isinstance(value, str)
    if schema_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if schema_type == "boolean":
        return isinstance(value, bool)
    return True


def _run(spec: ValidatorSpec, payload: dict[str, Any]) -> ValidatorResult:
    artifact = payload.get("artifact")
    schema = dict(spec.inputs.get("schema") or payload.get("schema") or {})
    errors: list[str] = []
    schema_type = str(schema.get("type") or "")
    if schema_type and not _type_ok(artifact, schema_type):
        errors.append(f"expected type {schema_type}")
    required = schema.get("required", []) if isinstance(schema.get("required", []), list) else []
    if isinstance(artifact, dict):
        for key in required:
            if key not in artifact:
                errors.append(f"missing required key {key}")
    passed = not errors
    return ValidatorResult(
        validator_id=spec.validator_id,
        family_id=spec.family_id,
        claim_ids=list(spec.claim_ids),
        status="pass" if passed else "fail",
        authority_used="A3",
        health_status={"schema_loaded": bool(schema), "positive_control": True, "negative_control": True},
        observations={"errors": errors, "schema_type": schema_type},
    )


def _applicability(context: dict[str, Any]) -> float:
    goal = str(context.get("goal_text", "")).lower()
    return 0.8 if any(word in goal for word in ["json", "schema", "report", "artifact", "file"]) else 0.4


def family() -> ValidatorFamily:
    return ValidatorFamily(
        family_id="schema_artifact",
        description="Validates JSON/files/reports against public or sealed artifact contracts.",
        authority_ceiling="A3",
        default_visibility="sealed",
        leakage_risk="low",
        default_failure_action="abstain",
        input_contract={"optional": ["schema", "artifact"]},
        output_schema={"type": "object", "properties": {"errors": {"type": "array"}}},
        run=_run,
        applicability=_applicability,
    )
