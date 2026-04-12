# Workstream 2 Proposal: Isolation Loader and Capability Enforcement

## Pre-Implementation Notes

- Freeze the runtime boundary once at `agintor-runtime-abi-v4` and `agintor-storage-v2`. Runtime manifests, deployment contracts, kernel bundles, capability exchange payloads, and template artifacts should move together.
- Treat `workspace_root` as a semantic runtime-owned root such as `runtime_workspace`, then resolve it per launch. Exported runtimes should not embed machine-specific host paths.
- `required_guarantees` are fail-closed. `desired_guarantees` may degrade to `best_effort`, but they must never be advertised as guaranteed.
- Runtime-wide isolation is an upper bound on solve-time power. Later tool sandboxes may tighten it, never relax it.

## agintor/schemas.py

```text
<<<<<<< SEARCH
class DeploymentContract(BaseModel):
    entry_command: str
    runtime_abi: str
    kernel_version: str = ""
    storage_schema_version: str = ""
    python_version: str
    supported_backends: List[str] = Field(default_factory=list)
    required_env_names: List[str] = Field(default_factory=list)
    required_env_any_of: List[List[str]] = Field(default_factory=list)
    environment_allowlist: List[str] = Field(default_factory=list)
    network_policy: str
    filesystem_policy: str
    dependency_digest_set: List[str] = Field(default_factory=list)
    container_image_digest: Optional[str] = None
    capability_flags: List[str] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)
    artifact_metadata: Optional[ArtifactMetadata] = None
=======
class IsolationGuaranteeLevel(str, Enum):
    GUARANTEED = "guaranteed"
    BEST_EFFORT = "best_effort"
    UNSUPPORTED = "unsupported"


class RuntimeIsolationGuarantee(BaseModel):
    name: Literal[
        "timeout_enforcement",
        "workspace_isolation",
        "environment_filtering",
        "process_cleanup",
        "network_disablement",
    ]
    level: IsolationGuaranteeLevel
    detail: str = ""


class RuntimeIsolationPolicy(BaseModel):
    timeout_envelope: Dict[str, Any] = Field(default_factory=dict)
    workspace_root: str
    environment_allowlist: List[str] = Field(default_factory=list)
    network_policy: str = "none"
    filesystem_policy: str
    required_guarantees: List[str] = Field(default_factory=list)
    desired_guarantees: List[str] = Field(default_factory=list)


class BackendIsolationReport(BaseModel):
    backend: str
    workspace_root: str = ""
    network_policy: str = ""
    filesystem_policy: str = ""
    guarantees: List[RuntimeIsolationGuarantee] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)


class BackendIsolationEvidence(BaseModel):
    backend: str
    resolved_workspace_root: str
    visible_env_names: List[str] = Field(default_factory=list)
    network_policy: str = ""
    filesystem_policy: str = ""
    guarantees: Dict[str, str] = Field(default_factory=dict)


class SideEffectReceipt(BaseModel):
    side_effect_id: str
    action_fingerprint: str
    idempotency_key: str
    action_kind: Literal[
        "tool_launch",
        "tool_completion",
        "provider_request",
        "provider_completion",
        "service_action",
        "filesystem_write",
    ]
    branch_id: str = ""
    request_digest: str
    backend: str
    status: str
    result_ref: Optional[str] = None
    replay_policy: str
    reconciliation_policy: str
    created_at: float


class CheckpointEnvelope(BaseModel):
    checkpoint_id: str
    runtime_abi: str
    storage_schema_version: str
    runtime_hash: str
    request_id: str
    plan_id: str
    task_id: str
    seed: int
    queued_frames: List[Dict[str, Any]] = Field(default_factory=list)
    branch_state: List[Dict[str, Any]] = Field(default_factory=list)
    branch_publications: List[Dict[str, Any]] = Field(default_factory=list)
    unresolved_goals: List[str] = Field(default_factory=list)
    artifact_refs: List[str] = Field(default_factory=list)
    handle_refs: List[str] = Field(default_factory=list)
    budget_state: Dict[str, Any] = Field(default_factory=dict)
    verifier_state: Dict[str, Any] = Field(default_factory=dict)
    working_state_summary: Dict[str, Any] = Field(default_factory=dict)
    trace_cursor: Dict[str, Any] = Field(default_factory=dict)
    side_effect_receipts: List[SideEffectReceipt] = Field(default_factory=list)


class DeploymentContract(BaseModel):
    entry_command: str
    runtime_abi: str
    kernel_version: str = ""
    storage_schema_version: str = ""
    python_version: str
    supported_backends: List[str] = Field(default_factory=list)
    required_env_names: List[str] = Field(default_factory=list)
    required_env_any_of: List[List[str]] = Field(default_factory=list)
    environment_allowlist: List[str] = Field(default_factory=list)
    network_policy: str
    filesystem_policy: str
    dependency_digest_set: List[str] = Field(default_factory=list)
    container_image_digest: Optional[str] = None
    capability_flags: List[str] = Field(default_factory=list)
    notes: List[str] = Field(default_factory=list)
    runtime_isolation: RuntimeIsolationPolicy
    artifact_metadata: Optional[ArtifactMetadata] = None
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
class RuntimePlan(BaseModel):
    plan_id: str
    goal_id: str
    runtime_abi: str
    kernel_version: str = ""
    storage_schema_version: str = ""
    seed_template: str
    mutable_files: List[str] = Field(default_factory=list)
    immutable_manifest: List[str] = Field(default_factory=list)
    runtime_profile: Dict[str, Any] = Field(default_factory=dict)
    provider_plan: ProviderPlan
    tooling_scope: List[str] = Field(default_factory=list)
    deployment_contract: DeploymentContract
    artifact_metadata: Optional[ArtifactMetadata] = None
=======
class RuntimePlan(BaseModel):
    plan_id: str
    goal_id: str
    runtime_abi: str
    kernel_version: str = ""
    storage_schema_version: str = ""
    seed_template: str
    mutable_files: List[str] = Field(default_factory=list)
    immutable_manifest: List[str] = Field(default_factory=list)
    runtime_profile: Dict[str, Any] = Field(default_factory=dict)
    provider_plan: ProviderPlan
    tooling_scope: List[str] = Field(default_factory=list)
    runtime_isolation: RuntimeIsolationPolicy
    deployment_contract: DeploymentContract
    artifact_metadata: Optional[ArtifactMetadata] = None
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
class CheckpointReference(BaseModel):
    ref: str
    task_id: str
    seed: int
    checkpoint_count: int = 0


class InspectRequest(BaseModel):
=======
class CheckpointReference(BaseModel):
    ref: str
    checkpoint_id: str = ""
    request_id: str = ""
    plan_id: str = ""
    task_id: str
    seed: int
    runtime_hash: str = ""
    checkpoint_count: int = 0


class InspectRequest(BaseModel):
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
class ResumeRequest(BaseModel):
    request_id: str
    checkpoint_ref: CheckpointReference
    prompt: Optional[str] = None
=======
class RuntimeResumeRequest(BaseModel):
    request_id: str
    checkpoint_ref: str
    trace_context: Dict[str, Any] = Field(default_factory=dict)
    reconciliation_policy: Literal["strict", "best_effort"] = "strict"
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
class CapabilityExchange(BaseModel):
    runtime_abi: str
    kernel_version: str
    storage_schema_version: str
    supported_backends: List[str] = Field(default_factory=list)
    tool_runtimes: List[str] = Field(default_factory=list)
    checkpoint_support: bool = True
    runtime_asset_capabilities: Dict[str, bool] = Field(default_factory=dict)
    side_effect_receipts: bool = False
    required_env_names: List[str] = Field(default_factory=list)
    required_env_any_of: List[List[str]] = Field(default_factory=list)
    capability_flags: List[str] = Field(default_factory=list)
=======
class CapabilityExchange(BaseModel):
    runtime_abi: str
    kernel_version: str
    storage_schema_version: str
    supported_backends: List[str] = Field(default_factory=list)
    selected_backend: str = ""
    tool_runtimes: List[str] = Field(default_factory=list)
    checkpoint_support: bool = True
    checkpoint_envelope_version: str = ""
    runtime_asset_capabilities: Dict[str, bool] = Field(default_factory=dict)
    side_effect_receipts: bool = False
    side_effect_action_kinds: List[str] = Field(default_factory=list)
    required_env_names: List[str] = Field(default_factory=list)
    required_env_any_of: List[List[str]] = Field(default_factory=list)
    runtime_isolation: Optional[RuntimeIsolationPolicy] = None
    backend_guarantees: List[BackendIsolationReport] = Field(default_factory=list)
    capability_flags: List[str] = Field(default_factory=list)
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
class RuntimeTaskInvocation(BaseModel):
    seed: int
    task: BenchmarkTask
=======
class RuntimeTaskInvocation(BaseModel):
    request_id: str
    seed: int
    task: BenchmarkTask
>>>>>>> REPLACE
```

Notes:
- `CheckpointEnvelope` and `SideEffectReceipt` are the storage-v2 contract additions that justify advertising receipt-aware resume and side-effect reuse.
- `CapabilityExchange` stays deterministic. Launch-specific evidence belongs in `BackendIsolationEvidence`, not in the equality-compared capability exchange payload.

## agintor/runtime_sdk/bundle.py

```text
<<<<<<< SEARCH
STORAGE_SCHEMA_VERSION = "agintor-storage-v1"
KERNEL_CAPABILITY_FLAGS = [
    "inspect",
    "run_batch",
    "checkpoint_refs",
    "provider_usage",
    "trace_refs",
]
=======
STORAGE_SCHEMA_VERSION = "agintor-storage-v2"
KERNEL_CAPABILITY_FLAGS = [
    "inspect",
    "run_batch",
    "resume",
    "checkpoint_refs",
    "checkpoint_envelopes",
    "provider_usage",
    "trace_refs",
    "side_effect_receipts",
    "runtime_isolation_contract",
]
>>>>>>> REPLACE
```

## agintor/runtime_builder.py

```text
<<<<<<< SEARCH
def _required_runtime_env_names(profile: RuntimeProfile) -> list[str]:
    return []


def _required_runtime_env_any_of(profile: RuntimeProfile) -> list[list[str]]:
    credential_group = [
        name
        for name in [
            str(profile.runtime_provider.api_key_env or "").strip(),
            str(profile.runtime_provider.api_key_file_env or "").strip(),
        ]
        if name
    ]
    return [credential_group] if credential_group else []
=======
def _required_runtime_env_names(profile: RuntimeProfile) -> list[str]:
    return []


def _required_runtime_env_any_of(profile: RuntimeProfile) -> list[list[str]]:
    credential_group = [
        name
        for name in [
            str(profile.runtime_provider.api_key_env or "").strip(),
            str(profile.runtime_provider.api_key_file_env or "").strip(),
        ]
        if name
    ]
    return [credential_group] if credential_group else []


def _build_runtime_isolation_policy(
    goal_spec: GoalSpec,
    profile: RuntimeProfile,
    *,
    environment_allowlist: list[str],
) -> RuntimeIsolationPolicy:
    network_policy = str(goal_spec.constraints.get("network_policy", "provider-only")).strip().lower() or "provider-only"
    filesystem_policy = str(goal_spec.constraints.get("filesystem_policy", "workspace-read-write")).strip().lower() or "workspace-read-write"
    required_guarantees = [
        "timeout_enforcement",
        "workspace_isolation",
        "environment_filtering",
    ]
    desired_guarantees = ["process_cleanup"]
    if network_policy == "none":
        required_guarantees.append("network_disablement")
    elif network_policy in {"provider-only", "restricted"}:
        desired_guarantees.append("network_disablement")
    return RuntimeIsolationPolicy(
        timeout_envelope={
            "run_timeout_s": int(profile.execution.latency_max),
            "tool_timeout_s": int(profile.tooling.t_slice),
            "grace_period_s": 5,
        },
        workspace_root="runtime_workspace",
        environment_allowlist=list(environment_allowlist),
        network_policy=network_policy,
        filesystem_policy=filesystem_policy,
        required_guarantees=sorted(set(required_guarantees)),
        desired_guarantees=sorted(set(desired_guarantees)),
    )


def _supported_backends_for_isolation(
    goal_spec: GoalSpec,
    runtime_isolation: RuntimeIsolationPolicy,
) -> list[str]:
    preferred = [
        str(item).strip().lower()
        for item in goal_spec.deployment_preferences.get("supported_backends", ["local", "docker"])
        if str(item).strip()
    ]
    supported = set(preferred or ["local", "docker"])
    if "network_disablement" in runtime_isolation.required_guarantees:
        supported.discard("local")
    return sorted(supported) or ["docker"]
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
    environment_allowlist = [name for name in environment_allowlist if name]
    return DeploymentContract(
        entry_command='agintor solve <runtime_dir> --prompt "<request>"',
        runtime_abi=RUNTIME_ABI_VERSION,
        kernel_version=KERNEL_VERSION,
        storage_schema_version=STORAGE_SCHEMA_VERSION,
        python_version=">=3.11",
        supported_backends=list(goal_spec.deployment_preferences.get("supported_backends", ["local", "docker"])),
        required_env_names=_required_runtime_env_names(profile),
        required_env_any_of=required_env_any_of,
        environment_allowlist=environment_allowlist,
        network_policy=str(goal_spec.constraints.get("network_policy", "provider-only")),
        filesystem_policy=str(goal_spec.constraints.get("filesystem_policy", "workspace-read-write")),
        dependency_digest_set=sorted(set(kernel_manifest.files.values())),
        capability_flags=[*KERNEL_CAPABILITY_FLAGS, "benchmark_mode", "prompt_mode"],
        notes=notes,
    )
=======
    environment_allowlist = [name for name in environment_allowlist if name]
    runtime_isolation = _build_runtime_isolation_policy(
        goal_spec,
        profile,
        environment_allowlist=environment_allowlist,
    )
    return DeploymentContract(
        entry_command='agintor solve <runtime_dir> --prompt "<request>"',
        runtime_abi=RUNTIME_ABI_VERSION,
        kernel_version=KERNEL_VERSION,
        storage_schema_version=STORAGE_SCHEMA_VERSION,
        python_version=">=3.11",
        supported_backends=_supported_backends_for_isolation(goal_spec, runtime_isolation),
        required_env_names=_required_runtime_env_names(profile),
        required_env_any_of=required_env_any_of,
        environment_allowlist=environment_allowlist,
        network_policy=str(goal_spec.constraints.get("network_policy", "provider-only")),
        filesystem_policy=str(goal_spec.constraints.get("filesystem_policy", "workspace-read-write")),
        dependency_digest_set=sorted(set(kernel_manifest.files.values())),
        capability_flags=[*KERNEL_CAPABILITY_FLAGS, "benchmark_mode", "prompt_mode", "resume"],
        notes=notes,
        runtime_isolation=runtime_isolation,
    )
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
    return RuntimePlan(
        plan_id=f"runtime.{stable_hash(goal_spec.goal_id, benchmark_plan.plan_id)[:12]}",
        goal_id=goal_spec.goal_id,
        runtime_abi=RUNTIME_ABI_VERSION,
        kernel_version=KERNEL_VERSION,
        storage_schema_version=STORAGE_SCHEMA_VERSION,
        seed_template=str(baseline_template_dir()),
        mutable_files=list(manifest.mutable_files),
        immutable_manifest=list(manifest.immutable_manifest),
        runtime_profile=runtime_profile_payload(profile),
        provider_plan=provider_plan,
        tooling_scope=_tooling_scope_from_suite(suite),
        deployment_contract=deployment_contract,
    )
=======
    return RuntimePlan(
        plan_id=f"runtime.{stable_hash(goal_spec.goal_id, benchmark_plan.plan_id)[:12]}",
        goal_id=goal_spec.goal_id,
        runtime_abi=RUNTIME_ABI_VERSION,
        kernel_version=KERNEL_VERSION,
        storage_schema_version=STORAGE_SCHEMA_VERSION,
        seed_template=str(baseline_template_dir()),
        mutable_files=list(manifest.mutable_files),
        immutable_manifest=list(manifest.immutable_manifest),
        runtime_profile=runtime_profile_payload(profile),
        provider_plan=provider_plan,
        tooling_scope=_tooling_scope_from_suite(suite),
        runtime_isolation=deployment_contract.runtime_isolation,
        deployment_contract=deployment_contract,
    )
>>>>>>> REPLACE
```

## agintor/project.py

```text
<<<<<<< SEARCH
    payload["runtime_abi"] = RUNTIME_ABI_VERSION
    payload["kernel_version"] = KERNEL_VERSION
    payload["storage_schema_version"] = STORAGE_SCHEMA_VERSION
    payload["required_env_names"] = required_env_names
    payload["required_env_any_of"] = required_env_any_of
    payload["environment_allowlist"] = environment_allowlist
    payload["dependency_digest_set"] = sorted(set(kernel_manifest.files.values()))
    payload["capability_flags"] = [*KERNEL_CAPABILITY_FLAGS, "benchmark_mode", "prompt_mode"]
    payload["notes"] = notes
=======
    payload["runtime_abi"] = RUNTIME_ABI_VERSION
    payload["kernel_version"] = KERNEL_VERSION
    payload["storage_schema_version"] = STORAGE_SCHEMA_VERSION
    payload["required_env_names"] = required_env_names
    payload["required_env_any_of"] = required_env_any_of
    payload["environment_allowlist"] = environment_allowlist
    payload["dependency_digest_set"] = sorted(set(kernel_manifest.files.values()))
    payload["capability_flags"] = [*KERNEL_CAPABILITY_FLAGS, "benchmark_mode", "prompt_mode", "resume"]
    payload["runtime_isolation"] = {
        "timeout_envelope": {
            "run_timeout_s": 120,
            "tool_timeout_s": 60,
            "grace_period_s": 5,
        },
        "workspace_root": "runtime_workspace",
        "environment_allowlist": environment_allowlist,
        "network_policy": payload.get("network_policy", "provider-only"),
        "filesystem_policy": payload.get("filesystem_policy", "workspace-read-write"),
        "required_guarantees": [
            "timeout_enforcement",
            "workspace_isolation",
            "environment_filtering",
        ],
        "desired_guarantees": ["process_cleanup"],
    }
    payload["notes"] = notes
>>>>>>> REPLACE
```

## agintor/runtime_loader.py

```text
<<<<<<< SEARCH
from .schemas import CapabilityExchange, DeploymentContract, KernelManifest, RuntimeManifest
=======
from .schemas import (
    BackendIsolationReport,
    CapabilityExchange,
    DeploymentContract,
    IsolationGuaranteeLevel,
    KernelManifest,
    RuntimeIsolationGuarantee,
    RuntimeIsolationPolicy,
    RuntimeManifest,
)
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
    STORAGE_SCHEMA_VERSION = "agintor-storage-v1"
from .utils import ast_node_count, file_digest, stable_hash

RUNTIME_ABI_VERSION = "agintor-runtime-abi-v3"
=======
    STORAGE_SCHEMA_VERSION = "agintor-storage-v2"
from .utils import ast_node_count, file_digest, stable_hash

RUNTIME_ABI_VERSION = "agintor-runtime-abi-v4"
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
def _validate_deployment_contract(
    runtime_path: Path,
    contract: DeploymentContract,
    *,
    runtime_backend: str | None = None,
    require_env_names: bool = False,
) -> None:
    if not _python_version_ok(contract.python_version):
        raise RuntimeLoadError(
            f"python version mismatch for {runtime_path}: required={contract.python_version} current={sys.version_info.major}.{sys.version_info.minor}"
        )
    if runtime_backend is not None:
        backend = str(runtime_backend).strip().lower()
        supported = {item.strip().lower() for item in contract.supported_backends}
        if backend and supported and backend not in supported:
            raise RuntimeLoadError(
                f"runtime backend {backend!r} is not supported by {runtime_path}; supported backends: {sorted(supported)}"
            )
    if require_env_names:
        missing = [name for name in contract.required_env_names if name and not os.environ.get(name)]
        if missing:
            raise RuntimeLoadError(
                f"missing required runtime environment variables for {runtime_path}: {', '.join(sorted(missing))}"
            )
        missing_any_of = []
        for group in contract.required_env_any_of:
            candidates = [str(name).strip() for name in group if str(name).strip()]
            if candidates and not any(os.environ.get(name) for name in candidates):
                missing_any_of.append(candidates)
        if missing_any_of:
            rendered = "; ".join(f"one of {', '.join(sorted(group))}" for group in missing_any_of)
            raise RuntimeLoadError(
                f"missing required runtime environment variables for {runtime_path}: {rendered}"
            )
=======
def _runtime_guarantees_for_backend(
    policy: RuntimeIsolationPolicy,
    backend: str,
) -> BackendIsolationReport:
    backend_key = str(backend or "").strip().lower()
    if backend_key == "local":
        levels = {
            "timeout_enforcement": IsolationGuaranteeLevel.GUARANTEED,
            "workspace_isolation": IsolationGuaranteeLevel.GUARANTEED,
            "environment_filtering": IsolationGuaranteeLevel.GUARANTEED,
            "process_cleanup": IsolationGuaranteeLevel.BEST_EFFORT,
            "network_disablement": IsolationGuaranteeLevel.UNSUPPORTED,
        }
        notes = [
            "Local execution may filter the environment and confine work to the dedicated workspace root.",
            "Local execution may not claim guaranteed network disablement or guaranteed descendant-process cleanup.",
        ]
    elif backend_key == "docker":
        levels = {
            "timeout_enforcement": IsolationGuaranteeLevel.GUARANTEED,
            "workspace_isolation": IsolationGuaranteeLevel.GUARANTEED,
            "environment_filtering": IsolationGuaranteeLevel.GUARANTEED,
            "process_cleanup": IsolationGuaranteeLevel.GUARANTEED,
            "network_disablement": (
                IsolationGuaranteeLevel.GUARANTEED
                if str(policy.network_policy or "").strip().lower() == "none"
                else IsolationGuaranteeLevel.BEST_EFFORT
            ),
        }
        notes = [
            "Docker claims only the guarantees enforced by launch flags and mounted paths.",
            "Do not advertise broader namespace, seccomp, or userns guarantees until the executor proves them.",
        ]
    else:
        levels = {
            "timeout_enforcement": IsolationGuaranteeLevel.UNSUPPORTED,
            "workspace_isolation": IsolationGuaranteeLevel.UNSUPPORTED,
            "environment_filtering": IsolationGuaranteeLevel.UNSUPPORTED,
            "process_cleanup": IsolationGuaranteeLevel.UNSUPPORTED,
            "network_disablement": IsolationGuaranteeLevel.UNSUPPORTED,
        }
        notes = [f"Unknown runtime backend {backend_key!r}."]
    guarantees = [
        RuntimeIsolationGuarantee(name=name, level=level)
        for name, level in levels.items()
    ]
    return BackendIsolationReport(
        backend=backend_key,
        workspace_root=policy.workspace_root,
        network_policy=policy.network_policy,
        filesystem_policy=policy.filesystem_policy,
        guarantees=guarantees,
        notes=notes,
    )


def _fail_on_unsatisfied_runtime_isolation(
    runtime_path: Path,
    policy: RuntimeIsolationPolicy,
    backend: str,
) -> BackendIsolationReport:
    report = _runtime_guarantees_for_backend(policy, backend)
    levels = {item.name: item.level for item in report.guarantees}
    missing_required = [
        name
        for name in policy.required_guarantees
        if levels.get(name) != IsolationGuaranteeLevel.GUARANTEED
    ]
    if missing_required:
        raise RuntimeLoadError(
            "runtime isolation guarantee mismatch for "
            f"{runtime_path}: backend={backend} required={sorted(missing_required)} "
            "actual="
            f"{ {name: str(levels.get(name, IsolationGuaranteeLevel.UNSUPPORTED)) for name in missing_required} } "
            "rebuild or re-export the runtime with a compatible backend contract"
        )
    return report


def _validate_deployment_contract(
    runtime_path: Path,
    contract: DeploymentContract,
    *,
    runtime_backend: str | None = None,
    require_env_names: bool = False,
) -> BackendIsolationReport | None:
    if not _python_version_ok(contract.python_version):
        raise RuntimeLoadError(
            f"python version mismatch for {runtime_path}: required={contract.python_version} current={sys.version_info.major}.{sys.version_info.minor}"
        )
    selected_report: BackendIsolationReport | None = None
    if runtime_backend is not None:
        backend = str(runtime_backend).strip().lower()
        supported = {item.strip().lower() for item in contract.supported_backends}
        if backend and supported and backend not in supported:
            raise RuntimeLoadError(
                f"runtime backend {backend!r} is not supported by {runtime_path}; supported backends: {sorted(supported)}. Rebuild or re-export the runtime."
            )
        if backend:
            selected_report = _fail_on_unsatisfied_runtime_isolation(
                runtime_path,
                contract.runtime_isolation,
                backend,
            )
    if require_env_names:
        missing = [name for name in contract.required_env_names if name and not os.environ.get(name)]
        if missing:
            raise RuntimeLoadError(
                f"missing required runtime environment variables for {runtime_path}: {', '.join(sorted(missing))}"
            )
        missing_any_of = []
        for group in contract.required_env_any_of:
            candidates = [str(name).strip() for name in group if str(name).strip()]
            if candidates and not any(os.environ.get(name) for name in candidates):
                missing_any_of.append(candidates)
        if missing_any_of:
            rendered = "; ".join(f"one of {', '.join(sorted(group))}" for group in missing_any_of)
            raise RuntimeLoadError(
                f"missing required runtime environment variables for {runtime_path}: {rendered}"
            )
    return selected_report
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
    _validate_deployment_contract(
        runtime_path,
        deployment_contract,
        runtime_backend=runtime_backend,
        require_env_names=require_env_names,
    )
=======
    selected_backend_report = _validate_deployment_contract(
        runtime_path,
        deployment_contract,
        runtime_backend=runtime_backend,
        require_env_names=require_env_names,
    )
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
    capability_exchange = CapabilityExchange(
        runtime_abi=RUNTIME_ABI_VERSION,
        kernel_version=kernel_manifest.kernel_version,
        storage_schema_version=kernel_manifest.storage_schema_version,
        supported_backends=list(deployment_contract.supported_backends),
        tool_runtimes=["python"],
        checkpoint_support=True,
        runtime_asset_capabilities={"traces": True, "checkpoints": True, "runtime_sdk": True},
        side_effect_receipts=False,
        required_env_names=list(deployment_contract.required_env_names),
        required_env_any_of=[list(group) for group in deployment_contract.required_env_any_of],
        capability_flags=list(deployment_contract.capability_flags or kernel_manifest.capability_flags),
    )
=======
    capability_exchange = CapabilityExchange(
        runtime_abi=RUNTIME_ABI_VERSION,
        kernel_version=kernel_manifest.kernel_version,
        storage_schema_version=kernel_manifest.storage_schema_version,
        supported_backends=list(deployment_contract.supported_backends),
        selected_backend=str(runtime_backend or "").strip().lower(),
        tool_runtimes=["python"],
        checkpoint_support=True,
        checkpoint_envelope_version="agintor.checkpoint_envelope.v1",
        runtime_asset_capabilities={
            "traces": True,
            "checkpoints": True,
            "checkpoint_envelopes": True,
            "runtime_sdk": True,
            "resume": True,
        },
        side_effect_receipts=True,
        side_effect_action_kinds=[
            "tool_launch",
            "tool_completion",
            "provider_request",
            "provider_completion",
            "service_action",
            "filesystem_write",
        ],
        required_env_names=list(deployment_contract.required_env_names),
        required_env_any_of=[list(group) for group in deployment_contract.required_env_any_of],
        runtime_isolation=deployment_contract.runtime_isolation,
        backend_guarantees=[
            _runtime_guarantees_for_backend(deployment_contract.runtime_isolation, backend)
            for backend in deployment_contract.supported_backends
        ],
        capability_flags=list(deployment_contract.capability_flags or kernel_manifest.capability_flags),
    )
>>>>>>> REPLACE
```

Notes:
- Keep the guarantee matrix centralized here so `runtime_host`, `container_runtime`, and `runtime_entry` consume one interpretation of local versus docker semantics.
- Do not claim `network_disablement` for `local`, and do not claim `process_cleanup` as guaranteed for `local`.

## agintor/runtime_host.py

```text
<<<<<<< SEARCH
        capability_exchange = self.inspect(runtime_dir)
        request = runtime_batch_request_for_tasks(
            request_id=f"run.{stable_hash(runtime_dir, len(task_runs), self.runtime_backend)[:12]}",
            runtime_backend=self.runtime_backend,
            task_runs=task_runs,
            budget_overrides=dict(budget_overrides or {}),
        )
=======
        capability_exchange = self.inspect(runtime_dir)
        request = runtime_batch_request_for_tasks(
            request_id=f"run.{stable_hash(runtime_dir, len(task_runs), self.runtime_backend)[:12]}",
            runtime_backend=self.runtime_backend,
            task_runs=[
                (
                    task,
                    seed,
                    f"benchmark.{task.task_id}.seed_{int(seed)}",
                )
                for task, seed in task_runs
            ],
            budget_overrides=dict(budget_overrides or {}),
        )
        self._preflight_execution_contract(
            runtime_dir,
            capability_exchange,
            provider=provider,
            runtime_profile=runtime_profile,
        )
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
    def _preflight_solve_contract(
        self,
        runtime_dir: str | Path,
        capability_exchange: CapabilityExchange,
        request: RuntimeSolveRequest,
        *,
        provider: ModelProvider,
        runtime_profile: object | None,
    ) -> None:
        if not self._provider_matches_runtime_profile(provider, runtime_profile):
            return
        if not self._request_requires_default_provider(request):
            return
        missing = [
            name
            for name in capability_exchange.required_env_names
            if str(name).strip() and not self._runtime_requirement_available(provider, str(name))
        ]
        missing_any_of = []
        for group in capability_exchange.required_env_any_of:
            candidates = [str(name).strip() for name in group if str(name).strip()]
            if candidates and not any(self._runtime_requirement_available(provider, name) for name in candidates):
                missing_any_of.append(candidates)
        if not missing and not missing_any_of:
            return
        parts = [", ".join(sorted(missing))] if missing else []
        parts.extend(f"one of {', '.join(sorted(group))}" for group in missing_any_of)
        raise RuntimeLoadError(
            f"missing required runtime environment variables for {runtime_dir}: {'; '.join(parts)}"
        )
=======
    def _preflight_execution_contract(
        self,
        runtime_dir: str | Path,
        capability_exchange: CapabilityExchange,
        *,
        provider: ModelProvider,
        runtime_profile: object | None,
    ) -> None:
        if capability_exchange.selected_backend and capability_exchange.selected_backend != self.runtime_backend:
            raise RuntimeLoadError(
                f"runtime capability exchange selected backend {capability_exchange.selected_backend!r}, expected {self.runtime_backend!r}"
            )
        isolation = capability_exchange.runtime_isolation
        if isolation is not None:
            guarantee_levels = {
                guarantee.name: guarantee.level
                for report in capability_exchange.backend_guarantees
                if report.backend == self.runtime_backend
                for guarantee in report.guarantees
            }
            missing_guarantees = [
                name
                for name in isolation.required_guarantees
                if str(guarantee_levels.get(name, "unsupported")) != "guaranteed"
            ]
            if missing_guarantees:
                raise RuntimeLoadError(
                    f"backend {self.runtime_backend!r} does not satisfy runtime isolation guarantees {sorted(missing_guarantees)} for {runtime_dir}"
                )
        if not self._provider_matches_runtime_profile(provider, runtime_profile):
            return
        missing = [
            name
            for name in capability_exchange.required_env_names
            if str(name).strip() and not self._runtime_requirement_available(provider, str(name))
        ]
        missing_any_of = []
        for group in capability_exchange.required_env_any_of:
            candidates = [str(name).strip() for name in group if str(name).strip()]
            if candidates and not any(self._runtime_requirement_available(provider, name) for name in candidates):
                missing_any_of.append(candidates)
        if not missing and not missing_any_of:
            return
        parts = [", ".join(sorted(missing))] if missing else []
        parts.extend(f"one of {', '.join(sorted(group))}" for group in missing_any_of)
        raise RuntimeLoadError(
            f"missing required runtime environment variables for {runtime_dir}: {'; '.join(parts)}"
        )
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
        capability_exchange = self.inspect(runtime_dir)
        self._preflight_solve_contract(
            runtime_dir,
            capability_exchange,
            request,
            provider=provider,
            runtime_profile=runtime_profile,
        )
=======
        capability_exchange = self.inspect(runtime_dir)
        self._preflight_execution_contract(
            runtime_dir,
            capability_exchange,
            provider=provider,
            runtime_profile=runtime_profile,
        )
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
    def _runtime_env(self, runtime_dir: Path) -> dict[str, str]:
        env = dict(os.environ)
        runtime_sdk = str((runtime_dir / KERNEL_BUNDLE_DIR).resolve())
        existing = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = runtime_sdk if not existing else runtime_sdk + os.pathsep + existing
        if self.sandbox_root is not None:
            env["AGINTOR_SANDBOX_CACHE_ROOT"] = str(self.sandbox_root)
        return env
=======
    def _runtime_env(
        self,
        runtime_dir: Path,
        *,
        capability_exchange: CapabilityExchange | None = None,
    ) -> dict[str, str]:
        env = {}
        runtime_sdk = str((runtime_dir / KERNEL_BUNDLE_DIR).resolve())
        existing = os.environ.get("PYTHONPATH", "")
        env["PYTHONPATH"] = runtime_sdk if not existing else runtime_sdk + os.pathsep + existing
        passthrough = {"PATH", "PATHEXT", "SYSTEMROOT", "COMSPEC", "TEMP", "TMP"}
        allowlist = set(passthrough)
        if capability_exchange and capability_exchange.runtime_isolation is not None:
            allowlist.update(capability_exchange.runtime_isolation.environment_allowlist)
            allowlist.update(capability_exchange.required_env_names)
            for group in capability_exchange.required_env_any_of:
                allowlist.update(group)
        else:
            allowlist.update(os.environ.keys())
        for env_name in sorted(allowlist):
            if env_name in os.environ:
                env[env_name] = os.environ[env_name]
        if self.sandbox_root is not None:
            env["AGINTOR_SANDBOX_CACHE_ROOT"] = str(self.sandbox_root)
        return env
>>>>>>> REPLACE
```

Notes:
- Add `resume()` to `RuntimeHost` and route it through the same preflight and filtered-environment path.
- `inspect()` may remain less strict about environment filtering because it exists to negotiate capability exchange, but solve, batch, and resume should use the filtered environment.
- Pass `capability_exchange` into `_runtime_env()` from `_run_local_batch()` and `_run_local_solve()` so local launches actually honor the exported allowlist. The inspect path may stay broader if capability negotiation still needs ambient env visibility.

## agintor/container_runtime.py

```text
<<<<<<< SEARCH
        command = [
            "docker",
            "run",
            "--rm",
            "-e",
            f"PYTHONPATH=/mnt/runtime/{KERNEL_BUNDLE_DIR}",
            "-v",
            f"{runtime_path}:/mnt/runtime:ro",
            "-v",
            f"{input_json.resolve()}:/mnt/input.json:ro",
            "-v",
            f"{output_json.parent.resolve()}:/mnt/output",
            "-v",
            f"{workspace_dir.resolve()}:/mnt/workspace",
            self.image_tag,
            "python",
            "-m",
            "agintor_runtime.runtime_entry",
            "inspect",
            "--runtime-dir",
            "/mnt/runtime",
            "--input-json",
            "/mnt/input.json",
            "--output-json",
            "/mnt/output/inspect_response.json",
        ]
=======
        backend_report_json = run_dir / "backend_report.json"
        backend_report_json.write_text(
            json.dumps(
                {
                    "backend": request.requested_backend,
                    "resolved_workspace_root": "/mnt/workspace",
                    "network_policy": "none" if request.requested_backend == "docker" else "provider-only",
                    "filesystem_policy": "workspace-read-write",
                    "guarantees": {
                        "timeout_enforcement": "guaranteed",
                        "workspace_isolation": "guaranteed",
                        "environment_filtering": "guaranteed",
                        "process_cleanup": "guaranteed",
                        "network_disablement": "guaranteed",
                    },
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        command = [
            "docker",
            "run",
            "--rm",
            "--name",
            f"agintor-runtime-{stable_hash(runtime_path, request.request_id)[:12]}",
            "--network",
            "none",
            "--read-only",
            "--tmpfs",
            "/tmp:rw,noexec,nosuid,size=67108864",
            "-e",
            f"PYTHONPATH=/mnt/runtime/{KERNEL_BUNDLE_DIR}",
            "-v",
            f"{runtime_path}:/mnt/runtime:ro",
            "-v",
            f"{input_json.resolve()}:/mnt/input.json:ro",
            "-v",
            f"{backend_report_json.resolve()}:/mnt/backend_report.json:ro",
            "-v",
            f"{output_json.parent.resolve()}:/mnt/output",
            "-v",
            f"{workspace_dir.resolve()}:/mnt/workspace",
            self.image_tag,
            "python",
            "-m",
            "agintor_runtime.runtime_entry",
            "inspect",
            "--runtime-dir",
            "/mnt/runtime",
            "--input-json",
            "/mnt/input.json",
            "--backend-report-json",
            "/mnt/backend_report.json",
            "--output-json",
            "/mnt/output/inspect_response.json",
        ]
>>>>>>> REPLACE
```

Notes:
- Reuse one helper for `inspect()`, `run_batch_protocol()`, and `solve_protocol()` that translates the selected `RuntimeIsolationPolicy` into Docker flags, a backend-evidence file, a subprocess timeout, and forced cleanup on timeout.
- Only claim `network_disablement` when the command actually uses `--network none`. Only claim `process_cleanup` as guaranteed when the executor can forcibly remove the named container on timeout or cancellation.
- Keep runtime mounts read-only except for `/mnt/workspace`. Add `--read-only` plus `--tmpfs /tmp` so the root filesystem cannot become the runtime write surface.

## agintor/runtime_sdk/runtime_entry.py

```text
<<<<<<< SEARCH
from .schemas import (
    BenchmarkTask,
    CapabilityExchange,
    InspectRequest,
    RunResult,
    RuntimeBatchRequest,
    RuntimeBatchResponse,
    RuntimeSolveRequest,
    RuntimeSolveResponse,
    RuntimeTaskInvocation,
    SolveRequest,
)
=======
from .schemas import (
    BackendIsolationEvidence,
    BenchmarkTask,
    CapabilityExchange,
    InspectRequest,
    RunResult,
    RuntimeBatchRequest,
    RuntimeBatchResponse,
    RuntimeResumeRequest,
    RuntimeSolveRequest,
    RuntimeSolveResponse,
    RuntimeTaskInvocation,
    SolveRequest,
)
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
    runtime = load_runtime(
        args.runtime_dir,
        runtime_profile=runtime_profile,
        runtime_backend=request.runtime_backend,
    )
=======
    runtime = load_runtime(
        args.runtime_dir,
        runtime_profile=runtime_profile,
        runtime_backend=request.runtime_backend,
    )
    if args.backend_report_json:
        backend_evidence = model_validate(
            BackendIsolationEvidence,
            json.loads(Path(args.backend_report_json).read_text(encoding="utf-8")),
        )
        if runtime.capability_exchange.runtime_isolation is not None:
            expected = runtime.capability_exchange.runtime_isolation
            if backend_evidence.backend != request.runtime_backend:
                raise ValueError("runtime backend evidence does not match the requested backend")
            if backend_evidence.resolved_workspace_root != str(Path(args.workspace).resolve()):
                raise ValueError("runtime backend evidence resolved a different workspace root than the active run workspace")
            required = {item.name: item.level for item in runtime.capability_exchange.backend_guarantees[0].guarantees if item.name in expected.required_guarantees}
            for guarantee_name, guarantee_level in required.items():
                if str(backend_evidence.guarantees.get(guarantee_name, "")) != str(guarantee_level):
                    raise ValueError(f"runtime backend guarantee mismatch for {guarantee_name}")
>>>>>>> REPLACE
```

```text
<<<<<<< SEARCH
    run_batch.add_argument("--workspace", required=True)
    run_batch.add_argument("--artifact-mode", default=ArtifactMode.NONE.value)
    run_batch.add_argument("--output-json", required=True)

    solve_parser = subparsers.add_parser("solve")
    solve_parser.add_argument("--runtime-dir", required=True)
    solve_parser.add_argument("--input-json", required=True)
    solve_parser.add_argument("--provider-json", required=True)
    solve_parser.add_argument("--profile-json")
    solve_parser.add_argument("--workspace", required=True)
    solve_parser.add_argument("--artifact-mode", default=ArtifactMode.NONE.value)
    solve_parser.add_argument("--output-json", required=True)
=======
    run_batch.add_argument("--workspace", required=True)
    run_batch.add_argument("--artifact-mode", default=ArtifactMode.NONE.value)
    run_batch.add_argument("--backend-report-json")
    run_batch.add_argument("--output-json", required=True)

    solve_parser = subparsers.add_parser("solve")
    solve_parser.add_argument("--runtime-dir", required=True)
    solve_parser.add_argument("--input-json", required=True)
    solve_parser.add_argument("--provider-json", required=True)
    solve_parser.add_argument("--profile-json")
    solve_parser.add_argument("--workspace", required=True)
    solve_parser.add_argument("--artifact-mode", default=ArtifactMode.NONE.value)
    solve_parser.add_argument("--backend-report-json")
    solve_parser.add_argument("--output-json", required=True)

    resume_parser = subparsers.add_parser("resume")
    resume_parser.add_argument("--runtime-dir", required=True)
    resume_parser.add_argument("--input-json", required=True)
    resume_parser.add_argument("--provider-json", required=True)
    resume_parser.add_argument("--profile-json")
    resume_parser.add_argument("--workspace", required=True)
    resume_parser.add_argument("--artifact-mode", default=ArtifactMode.NONE.value)
    resume_parser.add_argument("--backend-report-json")
    resume_parser.add_argument("--output-json", required=True)
>>>>>>> REPLACE
```

Notes:
- Add `inspect_parser.add_argument("--backend-report-json")` or remove the flag from the Docker inspect path; the CLI surface and container executor need to agree.
- Apply the same backend-evidence recheck in `_run_batch()` and `_resume()`.
- Add `_resume(args)` plus `if args.command == "resume": return _resume(args)` in `main()`. `resume` should enter through the same bundled entrypoint and isolation validation as `solve`, then hand the envelope to the runtime-owned resume path implemented alongside the checkpoint-envelope work.

## agintor/templates/baseline_runtime/runtime_manifest.json

```text
<<<<<<< SEARCH
    "runtime_abi": "agintor-runtime-abi-v3",
    "kernel_version": "agintor-kernel-v1",
    "storage_schema_version": "agintor-storage-v1",
=======
    "runtime_abi": "agintor-runtime-abi-v4",
    "kernel_version": "agintor-kernel-v1",
    "storage_schema_version": "agintor-storage-v2",
>>>>>>> REPLACE
```

## agintor/templates/baseline_runtime/deployment_contract.json

```text
<<<<<<< SEARCH
{
  "entry_command": "agintor solve <runtime_dir> --prompt \"<request>\"",
  "runtime_abi": "agintor-runtime-abi-v3",
  "kernel_version": "agintor-kernel-v1",
  "storage_schema_version": "agintor-storage-v1",
  "python_version": ">=3.11",
  "supported_backends": [
    "local",
    "docker"
  ],
  "required_env_names": [],
  "environment_allowlist": [],
  "network_policy": "provider-only",
  "filesystem_policy": "workspace-read-write",
  "dependency_digest_set": [],
  "container_image_digest": null,
  "capability_flags": [
    "inspect",
    "run_batch",
    "checkpoint_refs",
    "provider_usage",
    "benchmark_mode",
    "prompt_mode"
  ],
  "notes": []
}
=======
{
  "entry_command": "agintor solve <runtime_dir> --prompt \"<request>\"",
  "runtime_abi": "agintor-runtime-abi-v4",
  "kernel_version": "agintor-kernel-v1",
  "storage_schema_version": "agintor-storage-v2",
  "python_version": ">=3.11",
  "supported_backends": [
    "local",
    "docker"
  ],
  "required_env_names": [],
  "required_env_any_of": [],
  "environment_allowlist": [],
  "network_policy": "provider-only",
  "filesystem_policy": "workspace-read-write",
  "dependency_digest_set": [],
  "container_image_digest": null,
  "capability_flags": [
    "inspect",
    "run_batch",
    "resume",
    "checkpoint_refs",
    "checkpoint_envelopes",
    "provider_usage",
    "trace_refs",
    "side_effect_receipts",
    "runtime_isolation_contract",
    "benchmark_mode",
    "prompt_mode"
  ],
  "runtime_isolation": {
    "timeout_envelope": {
      "run_timeout_s": 120,
      "tool_timeout_s": 60,
      "grace_period_s": 5
    },
    "workspace_root": "runtime_workspace",
    "environment_allowlist": [],
    "network_policy": "provider-only",
    "filesystem_policy": "workspace-read-write",
    "required_guarantees": [
      "timeout_enforcement",
      "workspace_isolation",
      "environment_filtering"
    ],
    "desired_guarantees": [
      "process_cleanup",
      "network_disablement"
    ]
  },
  "notes": []
}
>>>>>>> REPLACE
```

## tests/test_runtime_host.py

```text
<<<<<<< SEARCH
def _capability_exchange() -> CapabilityExchange:
    return CapabilityExchange(
        runtime_abi="agintor-runtime-abi-v3",
        kernel_version="agintor-kernel-v1",
        storage_schema_version="agintor-storage-v1",
        supported_backends=["local", "docker"],
        tool_runtimes=["python"],
        checkpoint_support=True,
        runtime_asset_capabilities={"traces": True, "checkpoints": True, "runtime_sdk": True},
        side_effect_receipts=False,
        required_env_names=[],
        required_env_any_of=[["AGINTOR_MAS_MINIMAX_API_KEY", "AGINTOR_MAS_MINIMAX_KEY_FILE"]],
        capability_flags=["inspect", "run_batch", "benchmark_mode", "prompt_mode"],
    )
=======
def _capability_exchange() -> CapabilityExchange:
    return CapabilityExchange(
        runtime_abi="agintor-runtime-abi-v4",
        kernel_version="agintor-kernel-v1",
        storage_schema_version="agintor-storage-v2",
        supported_backends=["local", "docker"],
        selected_backend="local",
        tool_runtimes=["python"],
        checkpoint_support=True,
        checkpoint_envelope_version="agintor.checkpoint_envelope.v1",
        runtime_asset_capabilities={
            "traces": True,
            "checkpoints": True,
            "checkpoint_envelopes": True,
            "runtime_sdk": True,
            "resume": True,
        },
        side_effect_receipts=True,
        side_effect_action_kinds=[
            "tool_launch",
            "tool_completion",
            "provider_request",
            "provider_completion",
            "service_action",
            "filesystem_write",
        ],
        required_env_names=[],
        required_env_any_of=[["AGINTOR_MAS_MINIMAX_API_KEY", "AGINTOR_MAS_MINIMAX_KEY_FILE"]],
        runtime_isolation={
            "timeout_envelope": {"run_timeout_s": 120, "tool_timeout_s": 60, "grace_period_s": 5},
            "workspace_root": "runtime_workspace",
            "environment_allowlist": [],
            "network_policy": "provider-only",
            "filesystem_policy": "workspace-read-write",
            "required_guarantees": [
                "timeout_enforcement",
                "workspace_isolation",
                "environment_filtering",
            ],
            "desired_guarantees": ["process_cleanup"],
        },
        backend_guarantees=[
            {
                "backend": "local",
                "workspace_root": "runtime_workspace",
                "network_policy": "provider-only",
                "filesystem_policy": "workspace-read-write",
                "guarantees": [
                    {"name": "timeout_enforcement", "level": "guaranteed"},
                    {"name": "workspace_isolation", "level": "guaranteed"},
                    {"name": "environment_filtering", "level": "guaranteed"},
                    {"name": "process_cleanup", "level": "best_effort"},
                    {"name": "network_disablement", "level": "unsupported"},
                ],
                "notes": [],
            }
        ],
        capability_flags=["inspect", "run_batch", "resume", "benchmark_mode", "prompt_mode"],
    )
>>>>>>> REPLACE
```

## Assumptions, Risks, and Important Context

- `workspace_root` should remain semantic in the exported contract. The launch path resolves it to a concrete path and writes `BackendIsolationEvidence` for runtime-side recheck.
- `network_policy="provider-only"` should not force `network_disablement` into `required_guarantees`; it only constrains the runtime-wide contract and later tool-level sandboxes.
- Keep capability exchange deterministic. Launch-specific evidence should never be folded back into the inspect payload that `RuntimeHost` compares between inspect and solve.
- Docker claims should stay narrow. `--network none` and `--read-only` justify the MVP guarantees in this workstream. Do not claim broader kernel-hardening properties until the executor actually enforces them.
- `local` should never satisfy required `network_disablement` or required `process_cleanup`. If a deployment contract requires either, host preflight and runtime post-launch recheck should both fail closed on `local`.
- `CheckpointEnvelope` and `SideEffectReceipt` justify `agintor-storage-v2`, but durable indexing, retention policy, and long-lived recovery surfaces remain Workstream 3 work.
- The `RuntimeTaskInvocation.request_id` change implies matching builder updates in `agintor/runtime_api.py`; wire benchmark batch and eval invocations to `benchmark.<task_id>.seed_<seed>` when that companion proposal lands.
