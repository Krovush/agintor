from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..authority.public_tasks import sealed_canary_digest, task_envelope_public_projection
from ..authority.roles import (
    assert_evaluator_contract_import_allowed,
    assert_sealed_authority,
)
from ..contracts.epochs import (
    CapabilityEpoch,
    DataState,
    EvaluatorAuthority,
    REPO_REPAIR_CAPABILITY_EPOCH,
    ResearchEpochManifest,
    TaskEnvelope,
    assert_task_bound_to_epoch,
)
from ..contracts.outcomes import (
    DiagnosticScore,
    OutcomeCost,
    OutcomeHealth,
    OutcomeReceipt,
    PairKey,
)
from ..core.identity import canonical_identity_digest
from ..core.versioning import RUNTIME_CONTRACT_VERSION


assert_evaluator_contract_import_allowed()


_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


class EvaluationContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)


def _digest(value: str, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _DIGEST_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return normalized


def _nonempty(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} may not be empty")
    return normalized


def _relative_path(value: str, field_name: str) -> str:
    normalized = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{field_name} must be a nonempty fixture-relative path")
    return path.as_posix()


class SealedFixtureRef(EvaluationContractModel):
    fixture_id: str
    uri: str
    fixture_digest: str
    public_snapshot_digest: str
    immutable: Literal[True] = True

    @field_validator("fixture_id", "uri")
    @classmethod
    def validate_nonempty(cls, value: str, info: Any) -> str:
        return _nonempty(value, info.field_name)

    @field_validator("fixture_digest", "public_snapshot_digest")
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _digest(value, info.field_name)


class HiddenCheck(EvaluationContractModel):
    check_id: str
    argv: tuple[str, ...]
    cwd: str = "."
    timeout_ms: int = Field(gt=0)
    expected_exit_codes: tuple[int, ...] = (0,)
    environment: tuple[tuple[str, str], ...] = ()

    @field_validator("check_id")
    @classmethod
    def validate_check_id(cls, value: str) -> str:
        return _nonempty(value, "check_id")

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value or any(not str(part).strip() for part in value):
            raise ValueError("hidden check argv must contain nonempty arguments")
        return tuple(str(part) for part in value)

    @field_validator("cwd")
    @classmethod
    def validate_cwd(cls, value: str) -> str:
        normalized = str(value or ".").strip().replace("\\", "/") or "."
        path = PurePosixPath(normalized)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("hidden check cwd must be fixture-relative")
        return path.as_posix()

    @model_validator(mode="after")
    def validate_check(self) -> "HiddenCheck":
        if not self.expected_exit_codes:
            raise ValueError("hidden check expected_exit_codes may not be empty")
        if len(self.expected_exit_codes) != len(set(self.expected_exit_codes)):
            raise ValueError("hidden check expected_exit_codes may not contain duplicates")
        names = [name for name, _ in self.environment]
        if len(names) != len(set(names)):
            raise ValueError("hidden check environment names may not contain duplicates")
        if any(not str(name).strip() for name in names):
            raise ValueError("hidden check environment names may not be empty")
        return self


class CompleteRepairScoring(EvaluationContractModel):
    rule: Literal[
        "patch_applies_and_public_and_hidden_checks_pass"
    ] = "patch_applies_and_public_and_hidden_checks_pass"
    solver_reported_success_authoritative: Literal[False] = False
    diagnostic_scores_authoritative: Literal[False] = False


class ExclusionRule(EvaluationContractModel):
    reason_code: str
    condition: Literal[
        "fixture_invalid",
        "environment_invalid",
        "evaluator_error",
        "budget_accounting_unknown",
    ]

    @field_validator("reason_code")
    @classmethod
    def validate_reason_code(cls, value: str) -> str:
        return _nonempty(value, "reason_code")


class SealedCanary(EvaluationContractModel):
    canary_id: str
    value: str
    value_digest: str = ""

    @field_validator("canary_id", "value")
    @classmethod
    def validate_nonempty(cls, value: str, info: Any) -> str:
        return _nonempty(value, info.field_name)

    @model_validator(mode="after")
    def validate_canary_digest(self) -> "SealedCanary":
        computed = sealed_canary_digest(self.value)
        if self.value_digest and self.value_digest != computed:
            raise ValueError("sealed canary value_digest does not match its value")
        if not self.value_digest:
            object.__setattr__(self, "value_digest", computed)
        return self


class EvaluationContract(EvaluationContractModel):
    runtime_contract_version: str = RUNTIME_CONTRACT_VERSION
    evaluation_contract_id: str
    evaluation_contract_digest: str = ""
    epoch_id: str
    epoch_manifest_digest: str
    capability_epoch: CapabilityEpoch = REPO_REPAIR_CAPABILITY_EPOCH
    data_state: DataState
    split_manifest_digest: str
    task_manifest_id: str
    task_manifest_digest: str
    sealed_fixture: SealedFixtureRef
    protected_paths: tuple[str, ...]
    hidden_checks: tuple[HiddenCheck, ...]
    scoring: CompleteRepairScoring = Field(default_factory=CompleteRepairScoring)
    exclusions: tuple[ExclusionRule, ...] = ()
    outcome_authority: EvaluatorAuthority
    canaries: tuple[SealedCanary, ...]

    @field_validator("runtime_contract_version")
    @classmethod
    def validate_contract_version(cls, value: str) -> str:
        if str(value) != RUNTIME_CONTRACT_VERSION:
            raise ValueError("evaluation contract runtime contract version mismatch")
        return str(value)

    @field_validator("evaluation_contract_id", "epoch_id", "task_manifest_id")
    @classmethod
    def validate_nonempty(cls, value: str, info: Any) -> str:
        return _nonempty(value, info.field_name)

    @field_validator(
        "epoch_manifest_digest",
        "split_manifest_digest",
        "task_manifest_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _digest(value, info.field_name)

    @field_validator("protected_paths")
    @classmethod
    def validate_protected_paths(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(_relative_path(path, "protected path") for path in value)
        if not normalized:
            raise ValueError("evaluation contract requires protected paths")
        if len(normalized) != len(set(normalized)):
            raise ValueError("protected paths may not contain duplicates")
        return normalized

    @model_validator(mode="after")
    def validate_evaluation_contract(self) -> "EvaluationContract":
        if not self.hidden_checks:
            raise ValueError("repo-repair-v1 evaluation requires hidden checks")
        check_ids = [check.check_id for check in self.hidden_checks]
        if len(check_ids) != len(set(check_ids)):
            raise ValueError("hidden check_id values must be unique")
        if not self.canaries:
            raise ValueError("evaluation contract requires at least one sealed canary")
        canary_ids = [canary.canary_id for canary in self.canaries]
        canary_values = [canary.value for canary in self.canaries]
        if len(canary_ids) != len(set(canary_ids)):
            raise ValueError("sealed canary_id values must be unique")
        if len(canary_values) != len(set(canary_values)):
            raise ValueError("sealed canary values must be unique")
        exclusion_codes = [rule.reason_code for rule in self.exclusions]
        if len(exclusion_codes) != len(set(exclusion_codes)):
            raise ValueError("evaluation exclusion reason codes must be unique")
        computed = evaluation_contract_digest(self)
        if self.evaluation_contract_digest and self.evaluation_contract_digest != computed:
            raise ValueError("evaluation_contract_digest does not match the sealed contract")
        if not self.evaluation_contract_digest:
            object.__setattr__(self, "evaluation_contract_digest", computed)
        return self


def evaluation_contract_identity_payload(
    contract: EvaluationContract | Mapping[str, Any],
) -> dict[str, Any]:
    if isinstance(contract, EvaluationContract):
        payload = contract.model_dump(mode="python", exclude_none=True)
    else:
        payload = dict(contract)
    payload.pop("evaluation_contract_digest", None)
    return payload


def evaluation_contract_digest(
    contract: EvaluationContract | Mapping[str, Any],
) -> str:
    return canonical_identity_digest(
        evaluation_contract_identity_payload(contract),
        domain="repo-repair-evaluation-contract",
    )


def assert_evaluation_contract_bound(
    contract: EvaluationContract,
    *,
    epoch: ResearchEpochManifest,
    task: TaskEnvelope,
) -> None:
    assert_task_bound_to_epoch(task, epoch)
    expected = {
        "runtime_contract_version": epoch.runtime_contract_version,
        "epoch_id": epoch.epoch_id,
        "epoch_manifest_digest": epoch.epoch_manifest_digest,
        "capability_epoch": epoch.capability_epoch,
        "data_state": task.data_state,
        "split_manifest_digest": task.split_manifest_digest,
        "task_manifest_id": task.task_manifest_id,
        "task_manifest_digest": task.task_manifest_digest,
    }
    for field_name, expected_value in expected.items():
        if getattr(contract, field_name) != expected_value:
            raise ValueError(f"evaluation contract crossed authority boundary for {field_name}")
    if contract.sealed_fixture.public_snapshot_digest != task.workspace_snapshot.digest:
        raise ValueError("evaluation fixture does not bind the public workspace snapshot digest")
    if contract.outcome_authority != epoch.evaluator_authority:
        raise ValueError("evaluation contract outcome authority is not pinned by the epoch")
    task_envelope_public_projection(
        task,
        canary_values=tuple(canary.value for canary in contract.canaries),
        canary_digests=tuple(canary.value_digest for canary in contract.canaries),
    )


def evaluation_canary_digests(contract: EvaluationContract) -> tuple[str, ...]:
    """Safe canary identities for boundary scanners; never returns canary values."""

    return tuple(canary.value_digest for canary in contract.canaries)


def load_evaluation_contract(path: str | Path) -> EvaluationContract:
    """Evaluator-only loader. Public process modules intentionally cannot import it."""

    assert_sealed_authority("load an EvaluationContract")
    source = Path(path)
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("evaluation contract must contain valid UTF-8 JSON") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("evaluation contract JSON root must be an object")
    return EvaluationContract.model_validate(payload)


def issue_outcome_receipt(
    *,
    contract: EvaluationContract,
    epoch: ResearchEpochManifest,
    task: TaskEnvelope,
    receipt_id: str,
    release_digest: str,
    release_manifest_digest: str,
    profile_digest: str,
    execution_mode: Literal["deterministic_replay", "live_provider"],
    live_inference_status: Literal["not_run", "completed", "failed"],
    real_inference_requests_sent: int,
    pair_key: PairKey,
    protocol_digest: str,
    compiler_digest: str,
    kernel_digest: str,
    tool_manifest_digest: str,
    provider_config_digest: str,
    decoding_policy_digest: str,
    price_schedule_digest: str,
    command_container_policy_digest: str,
    evaluator_environment_digest: str,
    patch_digest: str,
    complete_repair: bool,
    health: OutcomeHealth,
    cost: OutcomeCost,
    exclusions: Sequence[str] = (),
    diagnostics: Sequence[DiagnosticScore] = (),
    issued_at_ms: int,
) -> OutcomeReceipt:
    """Issue the only promotion-capable V1 outcome from evaluator-owned inputs."""

    assert_sealed_authority("issue an evaluator-owned OutcomeReceipt")
    assert_evaluation_contract_bound(contract, epoch=epoch, task=task)
    if pair_key.task_manifest_id != task.task_manifest_id:
        raise ValueError("PairKey task does not match the evaluation task")
    if pair_key.provider_config_digest != epoch.deployment.provider_config_digest:
        raise ValueError("PairKey provider configuration is not pinned by the epoch")
    return OutcomeReceipt(
        receipt_id=receipt_id,
        execution_mode=execution_mode,
        live_inference_status=live_inference_status,
        real_inference_requests_sent=real_inference_requests_sent,
        data_state=task.data_state,
        epoch_id=epoch.epoch_id,
        epoch_manifest_digest=epoch.epoch_manifest_digest,
        release_digest=release_digest,
        release_manifest_digest=release_manifest_digest,
        profile_digest=profile_digest,
        split_manifest_digest=task.split_manifest_digest,
        task_manifest_id=task.task_manifest_id,
        task_manifest_digest=task.task_manifest_digest,
        evaluation_contract_id=contract.evaluation_contract_id,
        evaluation_contract_digest=contract.evaluation_contract_digest,
        evaluator_id=contract.outcome_authority.evaluator_id,
        evaluator_identity_digest=contract.outcome_authority.evaluator_identity_digest,
        evaluation_policy_digest=contract.outcome_authority.evaluation_policy_digest,
        pair_key=pair_key,
        protocol_digest=protocol_digest,
        compiler_digest=compiler_digest,
        kernel_digest=kernel_digest,
        tool_manifest_digest=tool_manifest_digest,
        provider_config_digest=provider_config_digest,
        decoding_policy_digest=decoding_policy_digest,
        price_schedule_digest=price_schedule_digest,
        command_container_policy_digest=command_container_policy_digest,
        evaluator_environment_digest=evaluator_environment_digest,
        patch_digest=patch_digest,
        complete_repair=complete_repair,
        health=health,
        exclusions=tuple(str(reason) for reason in exclusions),
        cost=cost,
        diagnostics=tuple(diagnostics),
        issued_at_ms=issued_at_ms,
    )


__all__ = [
    "CompleteRepairScoring",
    "EvaluationContract",
    "EvaluationContractModel",
    "ExclusionRule",
    "HiddenCheck",
    "SealedCanary",
    "SealedFixtureRef",
    "assert_evaluation_contract_bound",
    "evaluation_canary_digests",
    "evaluation_contract_digest",
    "evaluation_contract_identity_payload",
    "issue_outcome_receipt",
    "load_evaluation_contract",
]
