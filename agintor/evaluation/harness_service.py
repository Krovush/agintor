from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..authority.public_tasks import assert_public_payload, task_envelope_public_projection
from ..authority.roles import current_process_role
from ..contracts.epochs import (
    REPO_REPAIR_TRUSTED_TOOL_IDS,
    ResearchEpochManifest,
    TaskEnvelope,
    assert_task_bound_to_epoch,
    ceilings_usage_within,
)
from ..contracts.outcomes import OutcomeHealth, PairKey, pair_key_digest
from ..contracts.promotion_proof import (
    EvaluatorOutcomeProofBinding,
    bind_evaluator_outcome_proof,
)
from ..contracts.run_evidence import RunEvidence, assert_no_resolved_credentials
from ..core.identity import canonical_identity_digest, evidence_digest
from ..runtime.api.composite_compiler import CompositeCompilationError, compile_composite_run_plan
from ..runtime.evidence import (
    EvidenceAssemblyError,
    bind_and_append_evaluator_proof,
    public_verification_action_digest,
    tool_manifest_digest,
)
from ..runtime.harness_profile import HarnessDeploymentProfile
from ..runtime.sdk.harness_release_loader import (
    HarnessReleaseLoadError,
    LoadedHarnessRelease,
    load_active_harness_release,
)
from ..storage.proof_records import (
    ImmutableProofRecordStore,
    ProofStoreError,
)
from ..utils import stable_hash
from .contracts import (
    EvaluationContract,
    assert_evaluation_contract_bound,
    evaluation_canary_digests,
    issue_outcome_receipt,
)
from .runners.repo_patch_backends import (
    IsolatedRepoPatchCommandBackend,
    RepoPatchExecutionBackend,
)
from .runners.repo_patch_runner import (
    RepoPatchCommand,
    RepoPatchEvaluatorRunner,
    RepoPatchFixture,
    environment_digest as repo_patch_environment_digest,
    repo_patch_fixture_digest,
    repo_snapshot_digest,
    validate_unified_diff_paths,
)


HARNESS_EVALUATION_SCHEMA_VERSION = "repo-repair-harness-evaluation-v1"
HARNESS_EVALUATION_DRY_RUN_SCHEMA_VERSION = "repo-repair-harness-evaluation-dry-run-v1"

_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_ENVIRONMENT_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_SECRET_ENVIRONMENT_MARKERS = ("API_KEY", "AUTH", "CREDENTIAL", "PASSWORD", "SECRET", "TOKEN")
_ISOLATED_BASE_ENVIRONMENT = {
    "LANG": "C",
    "LC_ALL": "C",
    "PYTHONHASHSEED": "0",
    "TZ": "UTC",
}


class HarnessEvaluationRejected(RuntimeError):
    """Fail-closed evaluator rejection with a stable, non-sealed reason code."""

    def __init__(self, code: str, message: str) -> None:
        self.code = str(code)
        super().__init__(message)


class HarnessEvaluationModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=True)


def _digest(value: str, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _DIGEST_RE.fullmatch(normalized):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return normalized


def _relative_ref(value: str, field_name: str) -> str:
    normalized = str(value or "").strip().replace("\\", "/")
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{field_name} must be a safe controlled-store-relative reference")
    return path.as_posix()


class HarnessEvaluationDigestAssertions(HarnessEvaluationModel):
    """Optional equality assertions; none of these values selects authority."""

    release_digest: str | None = None
    release_manifest_digest: str | None = None
    epoch_manifest_digest: str | None = None
    task_manifest_digest: str | None = None
    protocol_digest: str | None = None
    compiled_semantic_digest: str | None = None
    dependency_manifest_digest: str | None = None
    compiler_digest: str | None = None
    kernel_digest: str | None = None
    tool_manifest_digest: str | None = None
    profile_digest: str | None = None
    provider_config_digest: str | None = None
    decoding_policy_digest: str | None = None
    price_schedule_digest: str | None = None
    command_container_policy_digest: str | None = None
    evaluation_contract_digest: str | None = None
    evaluator_environment_digest: str | None = None
    patch_digest: str | None = None

    @field_validator("*")
    @classmethod
    def validate_optional_digest(cls, value: str | None, info: Any) -> str | None:
        return None if value is None else _digest(value, info.field_name)


class HarnessEvaluationIdentity(HarnessEvaluationModel):
    release_digest: str
    release_manifest_digest: str
    epoch_manifest_digest: str
    task_manifest_digest: str
    pair_key_digest: str
    protocol_digest: str
    compiled_semantic_digest: str
    dependency_manifest_digest: str
    compiler_digest: str
    kernel_digest: str
    tool_manifest_digest: str
    profile_digest: str
    provider_config_digest: str
    decoding_policy_digest: str
    price_schedule_digest: str
    command_container_policy_digest: str
    evaluation_contract_digest: str
    fixture_digest: str
    evaluator_environment_digest: str
    patch_digest: str

    @field_validator("*")
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _digest(value, info.field_name)


class HarnessEvaluationCommandPlan(HarnessEvaluationModel):
    phase: Literal["patch_apply", "public_check", "sealed_check"]
    name: str = Field(min_length=1)
    argv: tuple[str, ...] = Field(min_length=1)
    working_directory: str
    environment: dict[str, str]
    timeout_s: float = Field(gt=0.0)
    expected_exit_codes: tuple[int, ...] = Field(min_length=1)

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(str(part) for part in value)
        if any(not part or "\x00" in part for part in normalized):
            raise ValueError("dry-run argv must contain nonempty NUL-free arguments")
        return normalized

    @field_validator("working_directory")
    @classmethod
    def validate_working_directory(cls, value: str) -> str:
        normalized = str(value or ".").strip().replace("\\", "/") or "."
        path = PurePosixPath(normalized)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("dry-run working directory must stay inside the scratch mount")
        return path.as_posix()

    @field_validator("expected_exit_codes")
    @classmethod
    def validate_exit_codes(cls, value: tuple[int, ...]) -> tuple[int, ...]:
        normalized = tuple(int(code) for code in value)
        if len(normalized) != len(set(normalized)):
            raise ValueError("dry-run expected exit codes must be unique")
        return normalized


class HarnessEvaluationMountPlan(HarnessEvaluationModel):
    source_kind: Literal["fresh_copy_of_immutable_sealed_fixture"] = (
        "fresh_copy_of_immutable_sealed_fixture"
    )
    source_snapshot_digest: str
    source_fixture_digest: str
    target: Literal["/workspace"] = "/workspace"
    repository_working_directory: Literal["/workspace/repo"] = "/workspace/repo"
    access: Literal["read_write_scratch"] = "read_write_scratch"
    immutable_source_not_mounted: Literal[True] = True
    network_policy: Literal["none"] = "none"

    @field_validator("source_snapshot_digest", "source_fixture_digest")
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _digest(value, info.field_name)


class HarnessEvaluationDryRunManifest(HarnessEvaluationModel):
    schema_version: Literal[HARNESS_EVALUATION_DRY_RUN_SCHEMA_VERSION] = (
        HARNESS_EVALUATION_DRY_RUN_SCHEMA_VERSION
    )
    status: Literal["not_run"] = "not_run"
    manifest_digest: str = ""
    identity: HarnessEvaluationIdentity
    execution_backend_id: str = Field(min_length=1)
    execution_backend_digest: str
    execution_backend_identity: dict[str, Any]
    mounts: tuple[HarnessEvaluationMountPlan, ...] = Field(min_length=1)
    commands: tuple[HarnessEvaluationCommandPlan, ...] = Field(min_length=1)
    backend_invocations: Literal[0] = 0

    @field_validator("execution_backend_digest")
    @classmethod
    def validate_backend_digest(cls, value: str) -> str:
        return _digest(value, "execution_backend_digest")

    @model_validator(mode="after")
    def bind_manifest_digest(self) -> "HarnessEvaluationDryRunManifest":
        payload = self.model_dump(mode="python", exclude={"manifest_digest"})
        computed = evidence_digest(
            {"kind": HARNESS_EVALUATION_DRY_RUN_SCHEMA_VERSION, **payload}
        )
        if self.manifest_digest and self.manifest_digest != computed:
            raise ValueError("harness evaluation dry-run manifest digest mismatch")
        if not self.manifest_digest:
            object.__setattr__(self, "manifest_digest", computed)
        return self


class HarnessEvaluationPublicSummary(HarnessEvaluationModel):
    schema_version: Literal[HARNESS_EVALUATION_SCHEMA_VERSION] = HARNESS_EVALUATION_SCHEMA_VERSION
    status: Literal["accepted"] = "accepted"
    complete_repair: bool
    release_digest: str
    release_manifest_digest: str
    epoch_manifest_digest: str
    task_manifest_digest: str
    pair_key_digest: str
    protocol_digest: str
    compiled_semantic_digest: str
    dependency_manifest_digest: str
    compiler_digest: str
    kernel_digest: str
    tool_manifest_digest: str
    profile_digest: str
    provider_config_digest: str
    decoding_policy_digest: str
    price_schedule_digest: str
    command_container_policy_digest: str
    run_evidence_digest: str
    isolated_evaluation_environment_digest: str
    patch_digest: str
    outcome_receipt_digest: str
    proof_record_digest: str
    proof_projection_digest: str
    isolated_runner_digest: str

    @field_validator(
        "release_digest",
        "release_manifest_digest",
        "epoch_manifest_digest",
        "task_manifest_digest",
        "pair_key_digest",
        "protocol_digest",
        "compiled_semantic_digest",
        "dependency_manifest_digest",
        "compiler_digest",
        "kernel_digest",
        "tool_manifest_digest",
        "profile_digest",
        "provider_config_digest",
        "decoding_policy_digest",
        "price_schedule_digest",
        "command_container_policy_digest",
        "run_evidence_digest",
        "isolated_evaluation_environment_digest",
        "patch_digest",
        "outcome_receipt_digest",
        "proof_record_digest",
        "proof_projection_digest",
        "isolated_runner_digest",
    )
    @classmethod
    def validate_digest(cls, value: str, info: Any) -> str:
        return _digest(value, info.field_name)


class ControlledProofReferences(HarnessEvaluationModel):
    store_manifest_ref: str = "store_manifest.json"
    proof_record_ref: str
    outcome_link_ref: str

    @field_validator("store_manifest_ref", "proof_record_ref", "outcome_link_ref")
    @classmethod
    def validate_ref(cls, value: str, info: Any) -> str:
        return _relative_ref(value, info.field_name)


class HarnessEvaluationResult(HarnessEvaluationModel):
    summary: HarnessEvaluationPublicSummary
    proof_references: ControlledProofReferences
    proof_binding: EvaluatorOutcomeProofBinding

    @model_validator(mode="after")
    def validate_proof_binding(self) -> "HarnessEvaluationResult":
        summary = self.summary
        binding = self.proof_binding
        references = self.proof_references
        expected = {
            "outcome_receipt_digest": binding.outcome_receipt.receipt_digest,
            "proof_record_digest": binding.proof_record_digest,
            "proof_projection_digest": binding.public_proof_digest,
            "run_evidence_digest": binding.run_evidence_digest,
        }
        crossed = [
            field_name
            for field_name, expected_value in expected.items()
            if getattr(summary, field_name) != expected_value
        ]
        if crossed:
            raise ValueError(
                "evaluation summary crossed evaluator proof binding: "
                + ", ".join(crossed)
            )
        if (
            references.store_manifest_ref != binding.store_manifest_ref
            or references.proof_record_ref != binding.proof_record_ref
            or references.outcome_link_ref != binding.outcome_link_ref
        ):
            raise ValueError("evaluation proof references crossed evaluator proof binding")
        return self


class HarnessEvaluationPublicResult(HarnessEvaluationModel):
    """Public projection of a controlled evaluator result."""

    summary: HarnessEvaluationPublicSummary
    proof_references: ControlledProofReferences


def harness_evaluation_public_result(
    result: HarnessEvaluationResult,
) -> HarnessEvaluationPublicResult:
    controlled = HarnessEvaluationResult.model_validate(
        result.model_dump(mode="python")
    )
    return HarnessEvaluationPublicResult(
        summary=controlled.summary,
        proof_references=controlled.proof_references,
    )


EvaluatorEpochResolver: TypeAlias = Callable[[str], ResearchEpochManifest]
ClockMs: TypeAlias = Callable[[], int]


@dataclass(frozen=True, slots=True)
class _EvaluationContext:
    release: LoadedHarnessRelease
    epoch: ResearchEpochManifest
    task: TaskEnvelope
    pair_key: PairKey
    fixture: RepoPatchFixture
    plan: Any
    identity: HarnessEvaluationIdentity


def _now_ms() -> int:
    return int(time.time() * 1000)


class HarnessEvaluationService:
    """Evaluator-only F1 service for one active immutable harness release.

    Provider, model, profile, protocol, compiler, kernel, and tool identities
    are read from the active release.  The call surface intentionally has no
    setters for any of them.
    """

    def __init__(
        self,
        *,
        project_root: str | Path,
        proof_store: ImmutableProofRecordStore,
        command_backend: RepoPatchExecutionBackend,
        epoch_resolver: EvaluatorEpochResolver,
        clock_ms: ClockMs = _now_ms,
    ) -> None:
        _require_evaluator_role()
        self.project_root = Path(project_root).expanduser().resolve()
        if not self.project_root.is_dir():
            raise HarnessEvaluationRejected(
                "project_missing",
                "harness evaluation requires an existing factory project",
            )
        if not isinstance(proof_store, ImmutableProofRecordStore):
            raise TypeError("proof_store must be an ImmutableProofRecordStore")
        if not callable(epoch_resolver):
            raise TypeError("epoch_resolver must resolve an evaluator epoch by release digest")
        if not callable(clock_ms):
            raise TypeError("clock_ms must be callable")
        _require_isolated_backend(command_backend)
        _assert_proof_store_separate(self.project_root, proof_store.root)
        self.proof_store = proof_store
        self.command_backend = command_backend
        self.epoch_resolver = epoch_resolver
        self.clock_ms = clock_ms
        self.runner = RepoPatchEvaluatorRunner(command_backend)

    def dry_run(
        self,
        *,
        contract: EvaluationContract,
        task: TaskEnvelope,
        submitted_unified_diff: str,
        pair_key: PairKey,
        digest_assertions: HarnessEvaluationDigestAssertions | None = None,
    ) -> HarnessEvaluationDryRunManifest:
        """Return the exact evaluator command plan without invoking the backend."""

        _require_evaluator_role()
        context = self._prepare(
            contract=contract,
            task=task,
            submitted_unified_diff=submitted_unified_diff,
            pair_key=pair_key,
            digest_assertions=digest_assertions,
        )
        commands = _command_plans(
            context.fixture,
            self.command_backend,
        )
        manifest = HarnessEvaluationDryRunManifest(
            identity=context.identity,
            execution_backend_id=self.command_backend.backend_id,
            execution_backend_digest=_backend_digest(self.command_backend),
            execution_backend_identity=_backend_identity(self.command_backend),
            mounts=(
                HarnessEvaluationMountPlan(
                    source_snapshot_digest=context.fixture.expected_repo_snapshot_digest,
                    source_fixture_digest=context.fixture.authority_fixture_digest,
                ),
            ),
            commands=commands,
        )
        try:
            assert_no_resolved_credentials(manifest.model_dump(mode="json"))
        except Exception as exc:
            raise HarnessEvaluationRejected(
                "dry_run_credential_material",
                "dry-run manifest contains forbidden resolved credential material",
            ) from exc
        return manifest

    def evaluate(
        self,
        *,
        contract: EvaluationContract,
        task: TaskEnvelope,
        submitted_unified_diff: str,
        run_evidence: RunEvidence,
        pair_key: PairKey,
        digest_assertions: HarnessEvaluationDigestAssertions | None = None,
    ) -> HarnessEvaluationResult:
        """Evaluate, issue evaluator authority, and append one immutable proof."""

        _require_evaluator_role()
        context = self._prepare(
            contract=contract,
            task=task,
            submitted_unified_diff=submitted_unified_diff,
            pair_key=pair_key,
            digest_assertions=digest_assertions,
        )
        evidence = _validated_run_evidence(run_evidence)
        _assert_public_inputs_safe(
            contract,
            task=context.task,
            submitted_unified_diff=submitted_unified_diff,
            pair_key=context.pair_key,
            run_evidence=evidence,
        )
        _assert_run_evidence_bound(
            evidence,
            context,
            submitted_unified_diff=submitted_unified_diff,
        )
        _assert_no_duplicate_outcome(self.proof_store, evidence)

        try:
            runner_result = self.runner.run(
                candidate_artifact=submitted_unified_diff,
                fixture=context.fixture,
            )
        except Exception as exc:
            raise HarnessEvaluationRejected(
                "isolated_evaluation_failed",
                "isolated evaluator execution failed closed",
            ) from exc

        _assert_runner_result(
            runner_result,
            context=context,
            backend=self.command_backend,
            submitted_unified_diff=submitted_unified_diff,
        )
        if runner_result.public_tests_passed != evidence.patch.public_verification_passed:
            raise HarnessEvaluationRejected(
                "runtime_evaluator_outcome_mismatch",
                "runtime public verification differs from isolated evaluator reproduction",
            )
        self._assert_release_still_active(context.release, contract)
        _assert_no_duplicate_outcome(self.proof_store, evidence)

        runner_health = runner_result.outcome_health(
            no_leakage=evidence.health.no_leakage,
            accounting_complete=evidence.health.accounting_complete,
        )
        outcome_health = OutcomeHealth(
            process_integrity=all(
                (
                    evidence.health.process_integrity,
                    evidence.health.context_integrity,
                    evidence.health.artifact_integrity,
                    evidence.health.tool_integrity,
                    runner_health.process_integrity,
                )
            ),
            no_leakage=evidence.health.no_leakage,
            environment_integrity=bool(
                evidence.health.environment_integrity
                and runner_health.environment_integrity
            ),
            evaluator_integrity=runner_health.evaluator_integrity,
            accounting_complete=evidence.health.accounting_complete,
        )
        if not outcome_health.passes_promotion_floor:
            raise HarnessEvaluationRejected(
                "evaluation_health_failed",
                "isolated evaluation did not satisfy the promotion health floor",
            )

        issued_at_ms = int(self.clock_ms())
        if issued_at_ms < 0:
            raise HarnessEvaluationRejected(
                "clock_invalid",
                "evaluator clock returned a negative timestamp",
            )
        receipt_id = "outcome." + evidence_digest(
            {
                "release": context.release.manifest.release_digest,
                "contract": contract.evaluation_contract_digest,
                "run_evidence": evidence.evidence_digest,
                "isolated_runner": runner_result.runner_digest,
            }
        )[:24]
        try:
            receipt = issue_outcome_receipt(
                contract=contract,
                epoch=context.epoch,
                task=context.task,
                receipt_id=receipt_id,
                release_digest=context.identity.release_digest,
                release_manifest_digest=context.identity.release_manifest_digest,
                profile_digest=context.identity.profile_digest,
                execution_mode=evidence.execution_mode,
                live_inference_status=evidence.live_inference_status,
                real_inference_requests_sent=evidence.real_inference_requests_sent,
                pair_key=context.pair_key,
                protocol_digest=context.identity.protocol_digest,
                compiler_digest=context.identity.compiler_digest,
                kernel_digest=context.identity.kernel_digest,
                tool_manifest_digest=context.identity.tool_manifest_digest,
                provider_config_digest=context.identity.provider_config_digest,
                decoding_policy_digest=context.identity.decoding_policy_digest,
                price_schedule_digest=context.identity.price_schedule_digest,
                command_container_policy_digest=(
                    context.identity.command_container_policy_digest
                ),
                evaluator_environment_digest=(
                    context.identity.evaluator_environment_digest
                ),
                patch_digest=context.identity.patch_digest,
                complete_repair=runner_result.complete_repair,
                health=outcome_health,
                cost=evidence.cost_ledger.cost,
                issued_at_ms=issued_at_ms,
            )
            proof_record, record_path = bind_and_append_evaluator_proof(
                epoch=context.epoch,
                task=context.task,
                run_evidence=evidence,
                outcome_receipt=receipt,
                store=self.proof_store,
            )
        except (ValueError, EvidenceAssemblyError, ProofStoreError) as exc:
            raise HarnessEvaluationRejected(
                "proof_binding_failed",
                "evaluator outcome could not be cross-bound to immutable run proof",
            ) from exc

        try:
            record_ref = record_path.resolve().relative_to(self.proof_store.root).as_posix()
        except ValueError as exc:
            raise HarnessEvaluationRejected(
                "proof_path_invalid",
                "proof store returned a record outside its controlled root",
            ) from exc
        proof_binding = bind_evaluator_outcome_proof(
            proof_record,
            proof_record_ref=record_ref,
            outcome_link_ref=f"outcome_links/{receipt.receipt_digest}.json",
        )
        result = HarnessEvaluationResult(
            summary=HarnessEvaluationPublicSummary(
                complete_repair=runner_result.complete_repair,
                release_digest=context.identity.release_digest,
                release_manifest_digest=context.identity.release_manifest_digest,
                epoch_manifest_digest=context.identity.epoch_manifest_digest,
                task_manifest_digest=context.identity.task_manifest_digest,
                pair_key_digest=context.identity.pair_key_digest,
                protocol_digest=context.identity.protocol_digest,
                compiled_semantic_digest=context.identity.compiled_semantic_digest,
                dependency_manifest_digest=context.identity.dependency_manifest_digest,
                compiler_digest=context.identity.compiler_digest,
                kernel_digest=context.identity.kernel_digest,
                tool_manifest_digest=context.identity.tool_manifest_digest,
                profile_digest=context.identity.profile_digest,
                provider_config_digest=context.identity.provider_config_digest,
                decoding_policy_digest=context.identity.decoding_policy_digest,
                price_schedule_digest=context.identity.price_schedule_digest,
                command_container_policy_digest=(
                    context.identity.command_container_policy_digest
                ),
                run_evidence_digest=evidence.evidence_digest,
                isolated_evaluation_environment_digest=(
                    context.identity.evaluator_environment_digest
                ),
                patch_digest=context.identity.patch_digest,
                outcome_receipt_digest=receipt.receipt_digest,
                proof_record_digest=proof_record.proof_record_digest,
                proof_projection_digest=proof_binding.public_proof_digest,
                isolated_runner_digest=runner_result.runner_digest,
            ),
            proof_references=ControlledProofReferences(
                proof_record_ref=record_ref,
                outcome_link_ref=f"outcome_links/{receipt.receipt_digest}.json",
            ),
            proof_binding=proof_binding,
        )
        try:
            payload = harness_evaluation_public_result(result).model_dump(mode="json")
            assert_public_payload(
                payload,
                canary_values=_canary_values(contract),
                canary_digests=evaluation_canary_digests(contract),
            )
            assert_no_resolved_credentials(payload)
        except Exception as exc:
            raise HarnessEvaluationRejected(
                "public_summary_refused",
                "evaluation result failed the strict public-summary boundary",
            ) from exc
        return result

    def _prepare(
        self,
        *,
        contract: EvaluationContract,
        task: TaskEnvelope,
        submitted_unified_diff: str,
        pair_key: PairKey,
        digest_assertions: HarnessEvaluationDigestAssertions | None,
    ) -> _EvaluationContext:
        normalized_contract = _validated_contract(contract)
        normalized_task = _validated_task(task)
        normalized_pair = _validated_pair_key(pair_key)
        patch = _validated_unified_diff(submitted_unified_diff, normalized_task)
        assertions = _validated_assertions(digest_assertions)
        _assert_public_inputs_safe(
            normalized_contract,
            task=normalized_task,
            submitted_unified_diff=patch,
            pair_key=normalized_pair,
        )
        forbidden_markers = (
            *_canary_values(normalized_contract),
            *evaluation_canary_digests(normalized_contract),
        )
        try:
            release = load_active_harness_release(
                self.project_root,
                forbidden_markers=forbidden_markers,
            )
        except HarnessReleaseLoadError as exc:
            raise HarnessEvaluationRejected(
                "active_release_invalid",
                "active harness release failed immutable public validation",
            ) from exc
        epoch = _resolve_release_epoch(release, self.epoch_resolver)
        _assert_backend_matches_release(self.command_backend, release)
        _assert_task_release_contract(
            contract=normalized_contract,
            task=normalized_task,
            epoch=epoch,
            release=release,
        )
        if normalized_pair.task_manifest_id != normalized_task.task_manifest_id:
            raise HarnessEvaluationRejected(
                "pair_task_mismatch",
                "PairKey task differs from the exact evaluation task",
            )
        if normalized_pair.provider_config_digest != release.manifest.deployment.provider_config_digest:
            raise HarnessEvaluationRejected(
                "pair_deployment_mismatch",
                "PairKey provider configuration differs from the active release",
            )
        try:
            plan = compile_composite_run_plan(
                normalized_task,
                release.protocol,
                release.dependencies,
            )
        except CompositeCompilationError as exc:
            raise HarnessEvaluationRejected(
                "released_protocol_compilation_failed",
                "active released protocol failed to compile for the exact task",
            ) from exc
        if (
            plan.task_envelope_digest != normalized_task.task_manifest_digest
            or plan.source_protocol_digest != release.manifest.protocol_source_digest
            or plan.dependency_manifest != release.dependencies
            or plan.dependency_manifest_digest != release.manifest.dependency_manifest_digest
        ):
            raise HarnessEvaluationRejected(
                "compiled_identity_mismatch",
                "compiled evaluation plan crossed task, protocol, or dependency identity",
            )
        try:
            fixture = RepoPatchFixture.from_evaluation_contract(
                normalized_contract,
                public_test_commands=normalized_task.public_reproduction,
            )
            validate_unified_diff_paths(
                patch,
                protected_paths=fixture.protected_paths,
            )
            actual_snapshot_digest = repo_snapshot_digest(fixture.repo_snapshot_path)
            computed_fixture_digest = repo_patch_fixture_digest(
                fixture,
                self.command_backend,
            )
            expected_environment_digest = repo_patch_environment_digest(
                fixture,
                self.command_backend,
            )
        except Exception as exc:
            raise HarnessEvaluationRejected(
                "fixture_or_patch_invalid",
                "evaluator fixture or submitted unified diff failed isolated preflight",
            ) from exc
        if actual_snapshot_digest != normalized_contract.sealed_fixture.public_snapshot_digest:
            raise HarnessEvaluationRejected(
                "fixture_snapshot_mismatch",
                "sealed fixture source differs from the task-bound immutable snapshot",
            )
        if computed_fixture_digest != normalized_contract.sealed_fixture.fixture_digest:
            raise HarnessEvaluationRejected(
                "fixture_identity_mismatch",
                "sealed fixture policy differs from evaluator authority",
            )
        patch_digest = canonical_identity_digest(patch, domain="final-unified-diff")
        identity = HarnessEvaluationIdentity(
            release_digest=release.manifest.release_digest,
            release_manifest_digest=release.manifest.manifest_digest,
            epoch_manifest_digest=release.manifest.epoch_manifest_digest,
            task_manifest_digest=normalized_task.task_manifest_digest,
            pair_key_digest=pair_key_digest(normalized_pair),
            protocol_digest=plan.source_protocol_digest,
            compiled_semantic_digest=plan.compiled_semantic_digest,
            dependency_manifest_digest=plan.dependency_manifest_digest,
            compiler_digest=release.dependencies.compiler.implementation_digest,
            kernel_digest=release.dependencies.kernel.implementation_digest,
            tool_manifest_digest=tool_manifest_digest(plan),
            profile_digest=release.manifest.profile_digest,
            provider_config_digest=release.manifest.deployment.provider_config_digest,
            decoding_policy_digest=release.manifest.deployment.decoding_policy_digest,
            price_schedule_digest=release.manifest.deployment.price_schedule_digest,
            command_container_policy_digest=(
                release.manifest.deployment.command_container_policy_digest
            ),
            evaluation_contract_digest=normalized_contract.evaluation_contract_digest,
            fixture_digest=computed_fixture_digest,
            evaluator_environment_digest=expected_environment_digest,
            patch_digest=patch_digest,
        )
        _assert_matching_digests(assertions, identity)
        return _EvaluationContext(
            release=release,
            epoch=epoch,
            task=normalized_task,
            pair_key=normalized_pair,
            fixture=fixture,
            plan=plan,
            identity=identity,
        )

    def _assert_release_still_active(
        self,
        prior: LoadedHarnessRelease,
        contract: EvaluationContract,
    ) -> None:
        try:
            current = load_active_harness_release(
                self.project_root,
                forbidden_markers=(
                    *_canary_values(contract),
                    *evaluation_canary_digests(contract),
                ),
            )
        except HarnessReleaseLoadError as exc:
            raise HarnessEvaluationRejected(
                "release_changed_during_evaluation",
                "active release did not remain immutable during evaluation",
            ) from exc
        if (
            current.pointer != prior.pointer
            or current.manifest != prior.manifest
            or current.runtime_identity != prior.runtime_identity
        ):
            raise HarnessEvaluationRejected(
                "release_changed_during_evaluation",
                "active release identity changed during evaluation",
            )


def _require_evaluator_role() -> None:
    if current_process_role() != "evaluator":
        raise HarnessEvaluationRejected(
            "evaluator_role_required",
            "harness evaluation service runs only in the evaluator process role",
        )


def _require_isolated_backend(backend: RepoPatchExecutionBackend) -> None:
    if (
        backend is None
        or getattr(backend, "is_isolated", None) is not True
        or getattr(backend, "backend_id", "") != IsolatedRepoPatchCommandBackend.backend_id
        or not callable(getattr(backend, "run", None))
        or not callable(getattr(backend, "command_argv", None))
    ):
        raise HarnessEvaluationRejected(
            "isolated_backend_required",
            "harness evaluation requires the E1 isolated repo-patch backend",
        )


def _assert_proof_store_separate(project_root: Path, proof_root: Path) -> None:
    release_root = (project_root / "releases").resolve()
    controlled = Path(proof_root).expanduser().resolve()
    if (
        controlled == release_root
        or controlled in release_root.parents
        or release_root in controlled.parents
    ):
        raise HarnessEvaluationRejected(
            "proof_store_release_overlap",
            "immutable evaluator proofs must live outside release generations",
        )


def _validated_contract(contract: EvaluationContract) -> EvaluationContract:
    if not isinstance(contract, EvaluationContract):
        raise HarnessEvaluationRejected(
            "evaluation_contract_required",
            "evaluation requires an evaluator-only EvaluationContract instance",
        )
    try:
        return EvaluationContract.model_validate(contract.model_dump(mode="python"))
    except Exception as exc:
        raise HarnessEvaluationRejected(
            "evaluation_contract_invalid",
            "EvaluationContract failed exact boundary validation",
        ) from exc


def _validated_task(task: TaskEnvelope) -> TaskEnvelope:
    if not isinstance(task, TaskEnvelope):
        raise HarnessEvaluationRejected(
            "task_envelope_required",
            "evaluation requires an exact TaskEnvelope instance",
        )
    try:
        normalized = TaskEnvelope.model_validate(task.model_dump(mode="python"))
    except Exception as exc:
        raise HarnessEvaluationRejected(
            "task_envelope_invalid",
            "TaskEnvelope failed exact boundary validation",
        ) from exc
    if normalized.data_state != "development":
        raise HarnessEvaluationRejected(
            "sealed_confirmation_forbidden",
            "F1 evaluation accepts development tasks only and returns no sealed feedback",
        )
    return normalized


def _validated_pair_key(pair_key: PairKey) -> PairKey:
    if not isinstance(pair_key, PairKey):
        raise HarnessEvaluationRejected(
            "pair_key_required",
            "evaluation requires an exact PairKey instance",
        )
    try:
        return PairKey.model_validate(pair_key.model_dump(mode="python"))
    except Exception as exc:
        raise HarnessEvaluationRejected(
            "pair_key_invalid",
            "PairKey failed exact boundary validation",
        ) from exc


def _validated_run_evidence(run_evidence: RunEvidence) -> RunEvidence:
    if not isinstance(run_evidence, RunEvidence):
        raise HarnessEvaluationRejected(
            "run_evidence_required",
            "evaluation requires an exact RunEvidence instance",
        )
    try:
        return RunEvidence.model_validate(run_evidence.model_dump(mode="python"))
    except Exception as exc:
        raise HarnessEvaluationRejected(
            "run_evidence_invalid",
            "RunEvidence failed exact boundary validation",
        ) from exc


def _validated_assertions(
    assertions: HarnessEvaluationDigestAssertions | None,
) -> HarnessEvaluationDigestAssertions:
    if assertions is None:
        return HarnessEvaluationDigestAssertions()
    if not isinstance(assertions, HarnessEvaluationDigestAssertions):
        raise HarnessEvaluationRejected(
            "digest_assertions_invalid",
            "only strict HarnessEvaluationDigestAssertions are accepted",
        )
    return HarnessEvaluationDigestAssertions.model_validate(
        assertions.model_dump(mode="python")
    )


def _validated_unified_diff(value: str, task: TaskEnvelope) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HarnessEvaluationRejected(
            "patch_empty",
            "submitted candidate must be a nonempty unified diff",
        )
    if "\x00" in value:
        raise HarnessEvaluationRejected("patch_invalid", "submitted unified diff contains NUL")
    if "\r" in value:
        raise HarnessEvaluationRejected(
            "patch_not_canonical",
            "submitted unified diff must use canonical LF line endings",
        )
    if len(value.encode("utf-8")) > task.ceilings.max_patch_bytes:
        raise HarnessEvaluationRejected(
            "patch_budget_exceeded",
            "submitted unified diff exceeds the task patch-byte ceiling",
        )
    return value


def _canary_values(contract: EvaluationContract) -> tuple[str, ...]:
    return tuple(canary.value for canary in contract.canaries)


def _assert_public_inputs_safe(
    contract: EvaluationContract,
    *,
    task: TaskEnvelope,
    submitted_unified_diff: str,
    pair_key: PairKey,
    run_evidence: RunEvidence | None = None,
) -> None:
    values = _canary_values(contract)
    digests = evaluation_canary_digests(contract)
    try:
        task_envelope_public_projection(
            task,
            canary_values=values,
            canary_digests=digests,
        )
        payload: dict[str, Any] = {
            "task": task.model_dump(mode="json"),
            "submitted_unified_diff": submitted_unified_diff,
            "pair_key": pair_key.model_dump(mode="json"),
        }
        if run_evidence is not None:
            payload["run_evidence"] = run_evidence.model_dump(mode="json")
        assert_public_payload(payload, canary_values=values, canary_digests=digests)
        assert_no_resolved_credentials(payload)
        markers = tuple(marker for marker in (*values, *digests) if marker)
        if _contains_literal_marker(payload, markers):
            raise ValueError("sealed marker detected in public evaluation input")
    except Exception as exc:
        raise HarnessEvaluationRejected(
            "public_input_boundary_failed",
            "task, patch, PairKey, or RunEvidence contains hidden or credential material",
        ) from exc


def _contains_literal_marker(value: Any, markers: Sequence[str]) -> bool:
    if isinstance(value, Mapping):
        return any(
            _contains_literal_marker(key, markers) or _contains_literal_marker(item, markers)
            for key, item in value.items()
        )
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_literal_marker(item, markers) for item in value)
    if isinstance(value, str):
        return any(marker in value for marker in markers)
    return False


def _resolve_release_epoch(
    release: LoadedHarnessRelease,
    resolver: EvaluatorEpochResolver,
) -> ResearchEpochManifest:
    try:
        resolved = resolver(release.manifest.epoch_manifest_digest)
        if not isinstance(resolved, ResearchEpochManifest):
            raise TypeError("resolver returned a non-epoch value")
        epoch = ResearchEpochManifest.model_validate(resolved.model_dump(mode="python"))
    except Exception as exc:
        raise HarnessEvaluationRejected(
            "epoch_authority_unavailable",
            "evaluator epoch authority could not resolve the active release digest",
        ) from exc
    public = release.epoch
    expected = {
        "runtime_contract_version": public.runtime_contract_version,
        "epoch_id": public.epoch_id,
        "epoch_manifest_digest": public.epoch_manifest_digest,
        "capability_epoch": public.capability_epoch,
        "task_manifest_digest": public.task_manifest_digest,
        "development_split_digest": public.development_split_digest,
        "deployment": public.deployment,
        "per_run_ceilings": public.per_run_ceilings,
        "search_envelope": public.search_envelope,
        "trusted_tools": public.trusted_tools,
        "mutation_surface": public.mutation_surface,
        "promotion_margins": public.promotion_margins,
        "stop_rule": public.stop_rule,
    }
    crossed = [
        field_name
        for field_name, expected_value in expected.items()
        if getattr(epoch, field_name) != expected_value
    ]
    if crossed:
        raise HarnessEvaluationRejected(
            "epoch_release_mismatch",
            "evaluator epoch authority differs from the active public release",
        )
    return epoch


def _assert_backend_matches_release(
    backend: RepoPatchExecutionBackend,
    release: LoadedHarnessRelease,
) -> None:
    try:
        profile = HarnessDeploymentProfile.model_validate(release.profile.profile)
        expected_environment = profile.command_container_policy.model_dump(mode="json")
        actual_environment = dict(_backend_identity(backend).get("environment", {}))
    except Exception as exc:
        raise HarnessEvaluationRejected(
            "release_environment_invalid",
            "active release command environment failed strict validation",
        ) from exc
    if actual_environment != expected_environment:
        raise HarnessEvaluationRejected(
            "backend_release_environment_mismatch",
            "injected isolated backend differs from the active release command environment",
        )


def _assert_task_release_contract(
    *,
    contract: EvaluationContract,
    task: TaskEnvelope,
    epoch: ResearchEpochManifest,
    release: LoadedHarnessRelease,
) -> None:
    try:
        assert_task_bound_to_epoch(task, epoch)
        assert_evaluation_contract_bound(contract, epoch=epoch, task=task)
    except Exception as exc:
        raise HarnessEvaluationRejected(
            "task_or_contract_mismatch",
            "task or EvaluationContract crossed active evaluator authority",
        ) from exc
    if contract.data_state != "development":
        raise HarnessEvaluationRejected(
            "sealed_confirmation_forbidden",
            "F1 evaluation does not expose sealed-confirmation outcomes",
        )
    public = release.epoch
    if (
        task.runtime_contract_version != public.runtime_contract_version
        or task.epoch_id != public.epoch_id
        or task.epoch_manifest_digest != public.epoch_manifest_digest
        or task.capability_epoch != public.capability_epoch
        or task.split_manifest_digest != public.development_split_digest
        or tuple(task.allowed_capabilities) != REPO_REPAIR_TRUSTED_TOOL_IDS
        or not task.ceilings.is_within(public.per_run_ceilings)
    ):
        raise HarnessEvaluationRejected(
            "task_release_mismatch",
            "exact task envelope differs from the active release authority",
        )


def _assert_matching_digests(
    assertions: HarnessEvaluationDigestAssertions,
    identity: HarnessEvaluationIdentity,
) -> None:
    values = identity.model_dump(mode="python")
    for field_name, assertion in assertions.model_dump(mode="python").items():
        if assertion is not None and assertion != values[field_name]:
            raise HarnessEvaluationRejected(
                "digest_assertion_mismatch",
                f"{field_name} assertion differs from the active evaluator identity",
            )


def _assert_run_evidence_bound(
    evidence: RunEvidence,
    context: _EvaluationContext,
    *,
    submitted_unified_diff: str,
) -> None:
    release = context.release
    identity = context.identity
    expected = {
        "capability_epoch": context.epoch.capability_epoch,
        "data_state": "development",
        "epoch_id": context.epoch.epoch_id,
        "epoch_manifest_digest": identity.epoch_manifest_digest,
        "release_digest": identity.release_digest,
        "release_manifest_digest": identity.release_manifest_digest,
        "profile_digest": identity.profile_digest,
        "split_manifest_digest": context.task.split_manifest_digest,
        "task_manifest_digest": identity.task_manifest_digest,
        "protocol_digest": identity.protocol_digest,
        "compiled_semantic_digest": identity.compiled_semantic_digest,
        "dependency_manifest_digest": identity.dependency_manifest_digest,
        "compiler_digest": identity.compiler_digest,
        "kernel_digest": identity.kernel_digest,
        "tool_manifest_digest": identity.tool_manifest_digest,
        "provider_config_digest": identity.provider_config_digest,
        "decoding_policy_digest": identity.decoding_policy_digest,
        "price_schedule_digest": identity.price_schedule_digest,
        "command_container_policy_digest": (
            identity.command_container_policy_digest
        ),
        "deployment_id": release.manifest.deployment.deployment_id,
        "provider": release.manifest.deployment.provider,
        "model": release.manifest.deployment.model,
    }
    crossed = [
        field_name
        for field_name, expected_value in expected.items()
        if getattr(evidence, field_name) != expected_value
    ]
    if crossed:
        raise HarnessEvaluationRejected(
            "run_evidence_identity_mismatch",
            "RunEvidence differs from active release, task, or deployment identities",
        )
    if evidence.pair_key != context.pair_key:
        raise HarnessEvaluationRejected(
            "run_evidence_pair_mismatch",
            "RunEvidence PairKey differs from the submitted exact PairKey",
        )
    if evidence.arm != "intact" or evidence.intervention_digest is not None:
        raise HarnessEvaluationRejected(
            "intervention_outcome_forbidden",
            "neutral/intervention evidence cannot receive promotion-capable evaluator authority",
        )
    if (
        evidence.environment.environment_id != context.pair_key.environment_id
        or evidence.environment.command_container_policy_digest
        != context.release.manifest.deployment.command_container_policy_digest
        or evidence.environment.workspace_snapshot_digest != context.task.workspace_snapshot.digest
        or evidence.environment.network_policy != "none"
        or evidence.environment.filesystem_policy != "scratch-workspace-only"
    ):
        raise HarnessEvaluationRejected(
            "run_evidence_environment_mismatch",
            "RunEvidence runtime environment differs from released public containment authority",
        )
    patch = evidence.patch
    if (
        patch.status != "emitted"
        or patch.observed is None
        or not isinstance(patch.observed.value, str)
        or patch.observed.value != submitted_unified_diff
        or patch.patch_digest != identity.patch_digest
        or patch.patch_bytes != len(patch.observed.value.encode("utf-8"))
        or patch.public_verification_passed is None
    ):
        raise HarnessEvaluationRejected(
            "run_evidence_patch_mismatch",
            "RunEvidence patch differs from the exact submitted unified diff",
        )
    if canonical_identity_digest(patch.observed.value, domain="final-unified-diff") != identity.patch_digest:
        raise HarnessEvaluationRejected(
            "run_evidence_patch_mismatch",
            "RunEvidence patch text differs from the submitted unified diff",
        )
    if not evidence.health.healthy:
        raise HarnessEvaluationRejected(
            "run_evidence_unhealthy",
            "unhealthy RunEvidence cannot receive evaluator outcome authority",
        )
    expected_termination_reason = (
        "success"
        if patch.public_verification_passed
        else "public_verification_failed"
    )
    if (
        evidence.termination.reason != expected_termination_reason
        or evidence.termination.success != patch.public_verification_passed
        or evidence.termination.final_call_id != context.plan.termination.final_actor_call_id
        or evidence.termination.final_patch_digest != identity.patch_digest
    ):
        raise HarnessEvaluationRejected(
            "run_evidence_partial",
            "partial or crossed RunEvidence cannot receive evaluator outcome authority",
        )
    ledger = evidence.cost_ledger
    if (
        not ledger.reconciled
        or ledger.active_reservations != 0
        or ledger.deadline_exceeded
        or ledger.provider_deadline_ms != context.task.ceilings.provider_deadline_ms
        or ledger.cost.unknown_dollars
        or not ledger.cost.within_epoch_envelope
    ):
        raise HarnessEvaluationRejected(
            "run_evidence_accounting_incomplete",
            "RunEvidence accounting is partial, unknown, or outside the frozen envelope",
        )
    usage = {
        "max_model_calls": ledger.cost.model_calls,
        "max_input_tokens": ledger.cost.input_tokens,
        "max_output_tokens": ledger.cost.output_tokens,
        "max_cached_tokens": ledger.cost.cached_tokens,
        "max_cache_write_tokens": ledger.cost.cache_write_tokens,
        "max_tool_calls": ledger.cost.tool_calls,
        "max_tool_output_bytes": ledger.cost.tool_output_bytes,
        "max_artifact_bytes": ledger.cost.artifact_bytes,
        "max_patch_bytes": ledger.cost.patch_bytes,
        "max_retries": ledger.cost.retries,
        "max_wall_time_ms": ledger.cost.wall_time_ms,
        "max_known_cost_usd": ledger.cost.known_cost_usd,
        "max_estimated_cost_usd": ledger.cost.estimated_cost_usd,
    }
    if not ceilings_usage_within(usage, context.task.ceilings):
        raise HarnessEvaluationRejected(
            "run_evidence_budget_exceeded",
            "RunEvidence exceeds the exact task ceiling",
        )
    plan_call_ids = {call.call_id for call in context.plan.actor_calls}
    context_call_ids = {item.call_id for item in evidence.contexts}
    provider_call_ids = {item.call_id for item in evidence.provider_calls}
    successful_call_ids = {
        item.call_id
        for item in evidence.provider_calls
        if item.status == "succeeded" and item.request_sent
    }
    if (
        context_call_ids != plan_call_ids
        or provider_call_ids != plan_call_ids
        or successful_call_ids != plan_call_ids
    ):
        raise HarnessEvaluationRejected(
            "run_evidence_partial",
            "RunEvidence does not cover every compiled actor call",
        )
    expected_public_invocations = {
        (action.step_id, public_verification_action_digest(action))
        for action in context.plan.public_verification.actions
    }
    terminal_receipts = tuple(
        receipt
        for receipt in evidence.tool_receipts
        if receipt.phase == "terminal_public_verification"
    )
    observed_public_invocations = {
        (receipt.verification_step_id, receipt.invocation_digest)
        for receipt in terminal_receipts
        if (
            receipt.call_id == context.plan.termination.final_actor_call_id
            and receipt.tool_id == "repo.public_test"
            and receipt.verification_step_id is not None
        )
    }
    statuses_match_claim = (
        all(receipt.status == "succeeded" for receipt in terminal_receipts)
        if patch.public_verification_passed
        else any(receipt.status != "succeeded" for receipt in terminal_receipts)
    )
    if (
        len(terminal_receipts) != len(expected_public_invocations)
        or observed_public_invocations != expected_public_invocations
        or not statuses_match_claim
    ):
        raise HarnessEvaluationRejected(
            "run_evidence_public_verification_incomplete",
            "RunEvidence public-verification receipts differ from its exact outcome claim",
        )


def _assert_runner_result(
    result: Any,
    *,
    context: _EvaluationContext,
    backend: RepoPatchExecutionBackend,
    submitted_unified_diff: str,
) -> None:
    expected_candidate_digest = stable_hash(
        "repo_patch.candidate_artifact",
        submitted_unified_diff,
    )
    invalid_candidate_or_integrity = (
        result.status not in {"pass", "fail"}
        or not result.applied
        or result.patch_apply is None
        or len(result.public_command_results)
        != len(context.fixture.public_test_commands)
        or len(result.hidden_command_results)
        != len(context.fixture.sealed_test_commands)
        or result.tampered_tests
        or result.tampered_paths
        or not result.source_snapshot_unchanged
        or not result.scratch_snapshot_matched
        or not result.fixture_identity_matched
        or not result.clean_copy_snapshot_unchanged
        or result.complete_repair != (result.status == "pass")
    )
    if invalid_candidate_or_integrity:
        raise HarnessEvaluationRejected(
            "candidate_rejected",
            "candidate was invalid or failed isolated evaluator integrity checks",
        )
    if (
        result.execution_backend_id != IsolatedRepoPatchCommandBackend.backend_id
        or result.execution_backend_digest != _backend_digest(backend)
    ):
        raise HarnessEvaluationRejected(
            "isolated_result_backend_mismatch",
            "isolated evaluator result crossed the injected backend identity",
        )
    if (
        result.repo_snapshot_digest != context.fixture.expected_repo_snapshot_digest
        or result.source_snapshot_digest_after != context.fixture.expected_repo_snapshot_digest
        or result.fixture_digest != context.identity.fixture_digest
        or result.evaluation_contract_digest != context.identity.evaluation_contract_digest
        or result.environment_digest != context.identity.evaluator_environment_digest
        or result.patch_digest != expected_candidate_digest
    ):
        raise HarnessEvaluationRejected(
            "isolated_result_identity_mismatch",
            "isolated evaluator result crossed fixture, patch, contract, or environment identity",
        )


def _assert_no_duplicate_outcome(
    store: ImmutableProofRecordStore,
    evidence: RunEvidence,
) -> None:
    try:
        for record in store.iter_records():
            prior = record.run_evidence
            if prior.evidence_digest == evidence.evidence_digest or (
                prior.pair_key == evidence.pair_key
                and prior.protocol_digest == evidence.protocol_digest
                and prior.patch.patch_digest == evidence.patch.patch_digest
            ):
                raise HarnessEvaluationRejected(
                    "duplicate_outcome",
                    "an immutable evaluator outcome already exists for this evidence or exact candidate pair",
                )
    except HarnessEvaluationRejected:
        raise
    except Exception as exc:
        raise HarnessEvaluationRejected(
            "proof_store_invalid",
            "existing proof records failed immutable validation",
        ) from exc


def _backend_identity(backend: RepoPatchExecutionBackend) -> dict[str, Any]:
    value = dict(backend.identity_payload)
    assert_no_resolved_credentials(value)
    return value


def _backend_digest(backend: RepoPatchExecutionBackend) -> str:
    return stable_hash("repo_patch.execution_backend", _backend_identity(backend))


def _command_environment(fixture: RepoPatchFixture, command: RepoPatchCommand) -> dict[str, str]:
    environment = dict(_ISOLATED_BASE_ENVIRONMENT)
    environment.update({str(key).upper(): str(value) for key, value in fixture.command_env.items()})
    environment.update({str(key).upper(): str(value) for key, value in command.env.items()})
    normalized = dict(sorted(environment.items()))
    for name, value in normalized.items():
        if not _ENVIRONMENT_NAME_RE.fullmatch(name):
            raise HarnessEvaluationRejected(
                "command_environment_invalid",
                "evaluation command contains an invalid environment name",
            )
        if any(marker in name for marker in _SECRET_ENVIRONMENT_MARKERS):
            raise HarnessEvaluationRejected(
                "command_environment_invalid",
                "evaluation command may not receive secret-bearing environment variables",
            )
        if "\x00" in value:
            raise HarnessEvaluationRejected(
                "command_environment_invalid",
                "evaluation command environment contains NUL",
            )
    assert_no_resolved_credentials(normalized)
    return normalized


def _command_plans(
    fixture: RepoPatchFixture,
    backend: RepoPatchExecutionBackend,
) -> tuple[HarnessEvaluationCommandPlan, ...]:
    plans: list[HarnessEvaluationCommandPlan] = [
        HarnessEvaluationCommandPlan(
            phase="patch_apply",
            name="apply_patch",
            argv=(
                *backend.git_argv,
                "apply",
                "--whitespace=nowarn",
                "--",
                "../.agintor_evaluator_input/candidate.patch",
            ),
            working_directory="repo",
            environment=dict(_ISOLATED_BASE_ENVIRONMENT),
            timeout_s=fixture.timeout_s,
            expected_exit_codes=(0,),
        )
    ]
    for phase, commands in (
        ("public_check", fixture.public_test_commands),
        ("sealed_check", fixture.sealed_test_commands),
    ):
        for command in commands:
            working_directory = "repo"
            if command.working_directory != ".":
                working_directory = f"repo/{command.working_directory}"
            plans.append(
                HarnessEvaluationCommandPlan(
                    phase=phase,
                    name=command.name,
                    argv=backend.command_argv(command.command),
                    working_directory=working_directory,
                    environment=_command_environment(fixture, command),
                    timeout_s=float(command.timeout_s or fixture.timeout_s),
                    expected_exit_codes=command.expected_exit_codes,
                )
            )
    return tuple(plans)


__all__ = [
    "ControlledProofReferences",
    "EvaluatorEpochResolver",
    "HARNESS_EVALUATION_DRY_RUN_SCHEMA_VERSION",
    "HARNESS_EVALUATION_SCHEMA_VERSION",
    "HarnessEvaluationCommandPlan",
    "HarnessEvaluationDigestAssertions",
    "HarnessEvaluationDryRunManifest",
    "HarnessEvaluationIdentity",
    "HarnessEvaluationMountPlan",
    "HarnessEvaluationPublicSummary",
    "HarnessEvaluationPublicResult",
    "HarnessEvaluationRejected",
    "HarnessEvaluationResult",
    "HarnessEvaluationService",
    "harness_evaluation_public_result",
]
