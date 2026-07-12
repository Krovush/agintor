from __future__ import annotations

import json
import re
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Literal

from ..contracts.epochs import (
    REPO_REPAIR_TRUSTED_TOOL_IDS,
    ResearchEpochManifest,
    TaskEnvelope,
    assert_task_bound_to_epoch,
)
from ..core.identity import canonical_identity_digest


PublicAudience = Literal["factory", "runtime", "proposer", "confirmation_runner"]

MAX_PUBLIC_TASK_BYTES = 1_048_576

_FORBIDDEN_KEY_PREFIXES = (
    "private_",
    "sealed_",
    "hidden_",
    "evaluator_",
    "oracle_private_",
    "gold_",
)
_FORBIDDEN_KEYS = {
    "answer_key",
    "evaluation_contract",
    "evaluation_contract_digest",
    "exclusions",
    "expected_answer",
    "expected_output",
    "fixture_path",
    "hidden_checks",
    "hidden_tests",
    "operation_dag",
    "outcome_authority",
    "protected_paths",
    "repair_decomposition",
    "repair_plan",
    "scoring",
    "sealed_fixture",
    "target_files",
    "target_location",
    "validation_plan",
}
_RESERVED_SOURCE_PARTS = {
    "evaluation-contract.json",
    "evaluation_contract.json",
    "hidden-tests.json",
    "hidden_tests.json",
    "sealed.json",
}
_RESERVED_SOURCE_DIRECTORIES = {
    "evaluator",
    "evaluator_mount",
    "evaluator_mounts",
    "hidden",
    "sealed",
    "sealed_fixture",
    "sealed_fixtures",
}


def _normalized_key(value: Any) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", str(value).casefold()).strip("_")
    return text


def _assert_public_keys(value: Any, *, path: str = "<root>") -> None:
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="python", exclude_none=True)
    if isinstance(value, Mapping):
        for raw_key, item in value.items():
            key = _normalized_key(raw_key)
            child_path = f"{path}.{raw_key}"
            if key in _FORBIDDEN_KEYS or key.startswith(_FORBIDDEN_KEY_PREFIXES):
                raise ValueError(f"sealed/evaluator field is forbidden in a public payload at {child_path}")
            _assert_public_keys(item, path=child_path)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for index, item in enumerate(value):
            _assert_public_keys(item, path=f"{path}[{index}]")


def sealed_canary_digest(value: Any) -> str:
    return canonical_identity_digest(value, domain="sealed-canary-value")


def _iter_scalar_values(value: Any):
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="python", exclude_none=True)
    if isinstance(value, Mapping):
        for key, item in value.items():
            yield key
            yield from _iter_scalar_values(item)
        return
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            yield from _iter_scalar_values(item)
        return
    yield value


def _assert_no_canaries(
    value: Any,
    *,
    canary_values: Sequence[str | bytes],
    canary_digests: Sequence[str],
) -> None:
    text_canaries = tuple(str(item) for item in canary_values if isinstance(item, str) and item)
    byte_canaries = tuple(bytes(item) for item in canary_values if isinstance(item, bytes) and item)
    digest_set = {str(item).strip().lower() for item in canary_digests if str(item).strip()}
    for scalar in _iter_scalar_values(value):
        if isinstance(scalar, str) and any(canary in scalar for canary in text_canaries):
            raise ValueError("sealed canary detected in public payload")
        if isinstance(scalar, (bytes, bytearray)) and any(canary in bytes(scalar) for canary in byte_canaries):
            raise ValueError("sealed canary detected in public payload")
        if digest_set and sealed_canary_digest(scalar) in digest_set:
            raise ValueError("sealed canary digest detected in public payload")


def assert_public_payload(
    value: Any,
    *,
    canary_values: Sequence[str | bytes] = (),
    canary_digests: Sequence[str] = (),
) -> None:
    """Fail closed when sealed structure or evaluator canaries cross a boundary."""

    _assert_public_keys(value)
    _assert_no_canaries(
        value,
        canary_values=canary_values,
        canary_digests=canary_digests,
    )


def _ceilings_projection(value: Any) -> dict[str, int | float]:
    return {
        "max_model_calls": value.max_model_calls,
        "max_input_tokens": value.max_input_tokens,
        "max_output_tokens": value.max_output_tokens,
        "max_cached_tokens": value.max_cached_tokens,
        "max_cache_write_tokens": value.max_cache_write_tokens,
        "max_tool_calls": value.max_tool_calls,
        "max_tool_output_bytes": value.max_tool_output_bytes,
        "max_artifact_bytes": value.max_artifact_bytes,
        "max_patch_bytes": value.max_patch_bytes,
        "max_retries": value.max_retries,
        "max_wall_time_ms": value.max_wall_time_ms,
        "provider_deadline_ms": value.provider_deadline_ms,
        "max_known_cost_usd": value.max_known_cost_usd,
        "max_estimated_cost_usd": value.max_estimated_cost_usd,
    }


def task_envelope_public_projection(
    task: TaskEnvelope,
    *,
    canary_values: Sequence[str | bytes] = (),
    canary_digests: Sequence[str] = (),
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "runtime_contract_version": task.runtime_contract_version,
        "task_manifest_id": task.task_manifest_id,
        "task_manifest_digest": task.task_manifest_digest,
        "epoch_id": task.epoch_id,
        "epoch_manifest_digest": task.epoch_manifest_digest,
        "capability_epoch": task.capability_epoch,
        "data_state": task.data_state,
        "split_manifest_digest": task.split_manifest_digest,
        "issue": task.issue,
        "workspace_snapshot": {
            "snapshot_id": task.workspace_snapshot.snapshot_id,
            "uri": task.workspace_snapshot.uri,
            "digest": task.workspace_snapshot.digest,
            "format": task.workspace_snapshot.format,
            "immutable": task.workspace_snapshot.immutable,
        },
        "public_reproduction": [
            {
                "step_id": step.step_id,
                "argv": list(step.argv),
                "cwd": step.cwd,
                "timeout_ms": step.timeout_ms,
                "expected_exit_codes": list(step.expected_exit_codes),
            }
            for step in task.public_reproduction
        ],
        "allowed_capabilities": list(task.allowed_capabilities),
        "ceilings": _ceilings_projection(task.ceilings),
    }
    assert_public_payload(
        payload,
        canary_values=canary_values,
        canary_digests=canary_digests,
    )
    return payload


def epoch_public_projection(epoch: ResearchEpochManifest) -> dict[str, Any]:
    """Return only deployment/search inputs needed by non-evaluator processes."""

    payload: dict[str, Any] = {
        "runtime_contract_version": epoch.runtime_contract_version,
        "epoch_id": epoch.epoch_id,
        "epoch_manifest_digest": epoch.epoch_manifest_digest,
        "capability_epoch": epoch.capability_epoch,
        "development_split_digest": epoch.development_split_digest,
        "deployment": {
            "deployment_id": epoch.deployment.deployment_id,
            "provider": epoch.deployment.provider,
            "model": epoch.deployment.model,
            "provider_config_digest": epoch.deployment.provider_config_digest,
            "decoding_policy_digest": epoch.deployment.decoding_policy_digest,
            "price_schedule_digest": epoch.deployment.price_schedule_digest,
        },
        "per_run_ceilings": _ceilings_projection(epoch.per_run_ceilings),
        "trusted_tools": [
            {
                "tool_id": tool.tool_id,
                "implementation_digest": tool.implementation_digest,
                "policy_digest": tool.policy_digest,
                "network_access": tool.network_access,
            }
            for tool in epoch.trusted_tools
        ],
        "mutation_surface": list(epoch.mutation_surface),
    }
    assert_public_payload(payload)
    return payload


def public_task_packet(
    task: TaskEnvelope,
    epoch: ResearchEpochManifest,
    *,
    audience: PublicAudience,
    canary_values: Sequence[str | bytes] = (),
    canary_digests: Sequence[str] = (),
) -> dict[str, Any]:
    assert_task_bound_to_epoch(task, epoch)
    if task.data_state == "sealed_confirmation" and audience in {"factory", "proposer"}:
        raise ValueError("sealed-confirmation tasks may not enter factory or proposer packets")
    payload = {
        "audience": audience,
        "epoch": epoch_public_projection(epoch),
        "task_envelope": task_envelope_public_projection(
            task,
            canary_values=canary_values,
            canary_digests=canary_digests,
        ),
    }
    assert_public_payload(
        payload,
        canary_values=canary_values,
        canary_digests=canary_digests,
    )
    return payload


def _epoch_value(epoch: Any, field_name: str) -> Any:
    if isinstance(epoch, Mapping):
        return epoch[field_name]
    return getattr(epoch, field_name)


def _optional_epoch_value(epoch: Any, field_name: str) -> Any | None:
    if isinstance(epoch, Mapping):
        return epoch.get(field_name)
    return getattr(epoch, field_name, None)


def _assert_task_bound_to_public_epoch(task: TaskEnvelope, epoch: Any) -> None:
    if task.runtime_contract_version != _epoch_value(
        epoch,
        "runtime_contract_version",
    ):
        raise ValueError("task and epoch runtime contract versions do not match")
    if task.epoch_id != _epoch_value(epoch, "epoch_id"):
        raise ValueError("task epoch_id does not match the pinned research epoch")
    if task.epoch_manifest_digest != _epoch_value(epoch, "epoch_manifest_digest"):
        raise ValueError("task epoch_manifest_digest does not match the pinned research epoch")
    if task.capability_epoch != _epoch_value(epoch, "capability_epoch"):
        raise ValueError("task capability epoch does not match the pinned research epoch")
    if task.data_state == "development":
        expected_split = _epoch_value(epoch, "development_split_digest")
    else:
        expected_split = _optional_epoch_value(epoch, "sealed_confirmation_split_digest")
        if expected_split is None:
            raise ValueError(
                "sealed-confirmation tasks require full evaluator epoch authority"
            )
    if task.split_manifest_digest != expected_split:
        raise ValueError("task split_manifest_digest does not match its epoch data state")
    if not task.ceilings.is_within(_epoch_value(epoch, "per_run_ceilings")):
        raise ValueError("task ceilings exceed the pinned research epoch envelope")


def _assert_public_source_path(path: Path) -> None:
    if path.suffix.casefold() != ".json":
        raise ValueError("public task source must be a JSON file")
    reserved = {part.casefold() for part in path.parts} & _RESERVED_SOURCE_PARTS
    if reserved:
        raise ValueError("public task loader refuses evaluator/sealed source paths")
    directory_parts = {_normalized_key(part) for part in path.parent.parts}
    if directory_parts & _RESERVED_SOURCE_DIRECTORIES:
        raise ValueError("public task loader refuses evaluator/sealed source paths")


def _path_prefixes(path: Path):
    current = Path(path.anchor) if path.anchor else Path()
    parts = path.parts[1:] if path.anchor else path.parts
    for part in parts:
        current = current / part
        yield current


def _is_link_or_junction(path: Path) -> bool:
    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    return bool(is_junction and is_junction())


def _assert_no_link_components(path: Path) -> None:
    for component in _path_prefixes(path):
        try:
            if _is_link_or_junction(component):
                raise ValueError("public task loader refuses symlink or junction source paths")
            if not component.exists():
                continue
        except OSError as exc:
            raise ValueError("public task loader could not validate source path components") from exc


def _public_task_source(path: str | Path) -> Path:
    source = Path(path).expanduser()
    _assert_public_source_path(source)
    _assert_no_link_components(source)
    try:
        resolved = source.resolve(strict=True)
    except OSError as exc:
        raise ValueError("public task source is missing or is not a regular file") from exc
    _assert_public_source_path(resolved)
    _assert_no_link_components(resolved)
    try:
        metadata = resolved.stat()
    except OSError as exc:
        raise ValueError("public task source is missing or is not a regular file") from exc
    if not stat.S_ISREG(metadata.st_mode):
        raise ValueError("public task source is missing or is not a regular file")
    if metadata.st_size > MAX_PUBLIC_TASK_BYTES:
        raise ValueError("public task exceeds the maximum serialized size")
    return resolved


def _decode_public_task_json(raw: bytes) -> dict[str, Any]:
    if len(raw) > MAX_PUBLIC_TASK_BYTES:
        raise ValueError("public task exceeds the maximum serialized size")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("public task must contain valid UTF-8 JSON") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("public task JSON root must be an object")
    return dict(payload)


def load_public_task(
    path: str | Path,
    *,
    epoch: Any,
    audience: PublicAudience,
    canary_values: Sequence[str | bytes] = (),
    canary_digests: Sequence[str] = (),
    allow_sealed_confirmation: bool = False,
) -> TaskEnvelope:
    """Load a task solely from its strict public JSON representation."""

    source = _public_task_source(path)
    raw = source.read_bytes()
    payload = _decode_public_task_json(raw)
    assert_public_payload(
        payload,
        canary_values=canary_values,
        canary_digests=canary_digests,
    )
    task = TaskEnvelope.model_validate(payload)
    _assert_task_bound_to_public_epoch(task, epoch)
    if task.data_state == "sealed_confirmation":
        if audience in {"factory", "proposer"}:
            raise ValueError("factory and proposer processes cannot load sealed-confirmation tasks")
        if not allow_sealed_confirmation or audience != "confirmation_runner":
            raise ValueError(
                "sealed-confirmation public tasks require the explicit confirmation-runner path"
            )
    task_envelope_public_projection(
        task,
        canary_values=canary_values,
        canary_digests=canary_digests,
    )
    if tuple(task.allowed_capabilities) != REPO_REPAIR_TRUSTED_TOOL_IDS:
        raise ValueError("public task capabilities are not the frozen repo-repair-v1 tool set")
    return task


__all__ = [
    "MAX_PUBLIC_TASK_BYTES",
    "PublicAudience",
    "assert_public_payload",
    "epoch_public_projection",
    "load_public_task",
    "public_task_packet",
    "sealed_canary_digest",
    "task_envelope_public_projection",
]
