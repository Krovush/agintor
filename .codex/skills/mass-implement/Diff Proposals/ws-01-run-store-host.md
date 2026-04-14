# Worker 01 Proposal: Durable Run Roots, Run Store, Host Transport, and Resume Targeting

Assumptions
- I am treating one durable run lineage as one solve or one grouped batch evaluation unit keyed by `batch_evaluation_unit_key(...)`. Transfer-scored episode rows therefore share one `run_id` / `run_root` / `attempt_id` until Worker 03 finalizes grouped batch ordering.
- Worker 02 owns the exact `runtime_state_snapshot` / `shell_state_snapshot` body and restore helpers. My diffs reserve the durable run identity and envelope plumbing they should plug into, but I am not redefining the detailed snapshot payload in this slice.
- Worker 03 owns frontier-only horizontal branching, branch persistence cadence, and final grouped batch ordering. I call out the interfaces those changes should consume, but I am not taking over their full branch scheduler patch here.

2026 research notes
- AWS’s durable execution guidance makes the key resume rule explicit: a caller-supplied execution name should behave as an idempotency key, completed steps should replay from checkpointed state, and launched-but-not-completed steps still require application-level idempotency keys before any retry or reconciliation. Source: [AWS Lambda durable execution idempotency](https://docs.aws.amazon.com/lambda/latest/dg/durable-execution-idempotency.html).
- Prefect’s current workflow docs emphasize that a flow run is a first-class lifecycle object with tracked run state and runtime context, not a transient temp directory. That reinforces separating run lineage from request provenance and persisting run/attempt metadata as typed state. Sources: [Prefect flows](https://docs.prefect.io/v3/concepts/flows), [Prefect runtime context](https://docs.prefect.io/v3/concepts/runtime-context), [Prefect quickstart](https://docs.prefect.io/v3/get-started/quickstart).

Cross-workstream interface notes
- Worker 02 should slot the exact snapshot payload under the `CheckpointEnvelope` identity shell proposed below and route side-effect receipt persistence through `RunStore.side_effects_dir`. They should not change the `run_id` / `run_root` / `attempt_id` contract after it lands.
- Worker 03 should sort grouped transfer-scored batch invocations by `(episode_order, task_id, request_id)` before execution and should preserve the shared run lineage stamped here for that group. If they decide to split episode groups into separate run roots later, that should be an explicit contract change, not an accidental side effect.
- `agintor/container_runtime.py` will need a follow-on mirror patch so docker path rewriting handles `run_root`, `latest_checkpoint_ref`, and run-root mounted checkpoint paths. I am not claiming the full docker executor diff in this worker file, but the schema and host changes below require it.

## `agintor/schemas.py`

Comment
- Separate runtime-owned run lineage from `request_id`.
- Keep `request_id` as user-task provenance only.
- Add typed run and attempt manifests, propagate run identity through request/result/checkpoint transport, and make resume target `run_ref` or `checkpoint_ref`.

File: `agintor/schemas.py`
<<<<<<< SEARCH
class SolveResult(BaseModel):
    request_id: str
    runtime_hash: str
    mode: str = "benchmark"
    artifact: Any
    status: str
    verification_status: str = "best_effort"
    summary: str
    checks: List[Dict[str, Any]] = Field(default_factory=list)
    trace_ref: Optional[str] = None
    checkpoint_ref: Optional[str] = None
    budget: Dict[str, Any] = Field(default_factory=dict)
    provider_usage: Dict[str, Any] = Field(default_factory=dict)
    faults: Dict[str, Any] = Field(default_factory=dict)
    recoverability: str = "none"
    verified: bool = False
    best_effort: bool = False


class RuntimeSolveRequest(BaseModel):
    request_id: str
    runtime_backend: str
    mode: Literal["benchmark", "user_request"]
    seed: int = 0
    task: Optional["BenchmarkTask"] = None
    solve_request: Optional[SolveRequest] = None
    budget_overrides: Dict[str, Any] = Field(default_factory=dict)
    trace_context: Optional[OpenAITraceContext] = None

    @root_validator(pre=False, allow_reuse=True)
    def validate_mode_payload(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        mode = values.get("mode")
        task = values.get("task")
        solve_request = values.get("solve_request")
        if mode == "benchmark" and task is None:
            raise ValueError("benchmark solve requests require a benchmark task")
        if mode == "user_request" and solve_request is None:
            raise ValueError("user_request solve requests require a solve_request payload")
        return values


class RunResult(BaseModel):
    request_id: str = ""
    plan_id: str = ""
    task_id: str
    seed: int
    artifact: Any
    verifier_score: float
    cost: float
    latency: float
    faults: int
    trace: List[Dict[str, Any]] = Field(default_factory=list)
    trace_context: Optional[OpenAITraceContext] = None
    trace_path: Optional[str] = None
    hard_invalid: bool = False
    invalid_reason: Optional[str] = None
    failure_kind: Optional[str] = None
    mode: Optional[str] = None
    lifecycle_state: Optional[str] = None
    created_tools: int = 0
    promoted_nodes: int = 0
    checks_used: int = 0
    model_calls: int = 0
    tokens_used: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    utility: Optional[float] = None
    checkpoint_ref: Optional[str] = None
=======
class SolveResult(BaseModel):
    request_id: str
    runtime_hash: str
    run_id: str = ""
    run_root: str = ""
    attempt_id: str = ""
    latest_checkpoint_ref: Optional[str] = None
    run_lifecycle_state: Optional[Literal["running", "paused", "completed", "failed", "pruned"]] = None
    run_resumable: bool = False
    run_prune_eligible: bool = False
    mode: str = "benchmark"
    artifact: Any
    status: str
    verification_status: str = "best_effort"
    summary: str
    checks: List[Dict[str, Any]] = Field(default_factory=list)
    trace_ref: Optional[str] = None
    checkpoint_ref: Optional[str] = None
    budget: Dict[str, Any] = Field(default_factory=dict)
    provider_usage: Dict[str, Any] = Field(default_factory=dict)
    faults: Dict[str, Any] = Field(default_factory=dict)
    recoverability: str = "none"
    verified: bool = False
    best_effort: bool = False


class RuntimeSolveRequest(BaseModel):
    request_id: str
    run_id: str = ""
    run_root: str = ""
    attempt_id: str = ""
    runtime_backend: str
    mode: Literal["benchmark", "user_request"]
    seed: int = 0
    task: Optional["BenchmarkTask"] = None
    solve_request: Optional[SolveRequest] = None
    budget_overrides: Dict[str, Any] = Field(default_factory=dict)
    trace_context: Optional[OpenAITraceContext] = None

    @root_validator(pre=False, allow_reuse=True)
    def validate_mode_payload(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        mode = values.get("mode")
        task = values.get("task")
        solve_request = values.get("solve_request")
        if mode == "benchmark" and task is None:
            raise ValueError("benchmark solve requests require a benchmark task")
        if mode == "user_request" and solve_request is None:
            raise ValueError("user_request solve requests require a solve_request payload")
        return values


class RunResult(BaseModel):
    request_id: str = ""
    plan_id: str = ""
    run_id: str = ""
    run_root: str = ""
    attempt_id: str = ""
    latest_checkpoint_ref: Optional[str] = None
    run_lifecycle_state: Optional[Literal["running", "paused", "completed", "failed", "pruned"]] = None
    run_resumable: bool = False
    run_prune_eligible: bool = False
    task_id: str
    seed: int
    artifact: Any
    verifier_score: float
    cost: float
    latency: float
    faults: int
    trace: List[Dict[str, Any]] = Field(default_factory=list)
    trace_context: Optional[OpenAITraceContext] = None
    trace_path: Optional[str] = None
    hard_invalid: bool = False
    invalid_reason: Optional[str] = None
    failure_kind: Optional[str] = None
    mode: Optional[str] = None
    lifecycle_state: Optional[str] = None
    created_tools: int = 0
    promoted_nodes: int = 0
    checks_used: int = 0
    model_calls: int = 0
    tokens_used: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    utility: Optional[float] = None
    checkpoint_ref: Optional[str] = None
>>>>>>> REPLACE

File: `agintor/schemas.py`
<<<<<<< SEARCH
class RuntimeManifest(BaseModel):
    runtime_id: str
    version: str
    policy_modules: Dict[str, str]
    mutable_files: List[str]
    immutable_manifest: List[str]
    metadata: Dict[str, Any] = Field(default_factory=dict)


class KernelManifest(BaseModel):
=======
class RuntimeManifest(BaseModel):
    runtime_id: str
    version: str
    policy_modules: Dict[str, str]
    mutable_files: List[str]
    immutable_manifest: List[str]
    metadata: Dict[str, Any] = Field(default_factory=dict)


class RunManifest(BaseModel):
    run_id: str
    run_root: str
    request_id: str = ""
    request_mode: Literal["benchmark", "user_request", "batch"] = "benchmark"
    runtime_hash: str = ""
    runtime_abi: str = ""
    storage_schema_version: str = ""
    runtime_backend: str = "local"
    task_id: Optional[str] = None
    seed: Optional[int] = None
    trace_context: Optional[OpenAITraceContext] = None
    current_attempt_id: Optional[str] = None
    latest_checkpoint_ref: Optional[str] = None
    lifecycle_state: Literal["running", "paused", "completed", "failed", "pruned"] = "running"
    resumable: bool = False
    prune_eligible: bool = False
    last_failure_kind: Optional[str] = None
    created_at: float = 0.0
    updated_at: float = 0.0


class AttemptManifest(BaseModel):
    attempt_id: str
    run_id: str
    run_root: str
    sequence_no: int
    launch_kind: Literal["solve", "run_batch", "resume"]
    lifecycle_state: Literal["running", "completed", "paused", "failed", "crashed", "cancelled"] = "running"
    resumed_from_checkpoint_ref: Optional[str] = None
    workspace_root: str
    latest_checkpoint_ref: Optional[str] = None
    failure_kind: Optional[str] = None
    started_at: float = 0.0
    updated_at: float = 0.0
    finished_at: Optional[float] = None


class KernelManifest(BaseModel):
>>>>>>> REPLACE

File: `agintor/schemas.py`
<<<<<<< SEARCH
class CheckpointReference(BaseModel):
    ref: str
    task_id: str = ""
    seed: int = 0
    request_id: str = ""
    plan_id: str = ""
    checkpoint_id: str = ""
    sequence_no: int = 0
    boundary: str = ""
    created_at: float = 0.0
    checkpoint_count: int = 0
    latest: bool = False
=======
class CheckpointReference(BaseModel):
    ref: str
    run_id: str = ""
    run_root: str = ""
    attempt_id: str = ""
    task_id: str = ""
    seed: int = 0
    request_id: str = ""
    plan_id: str = ""
    checkpoint_id: str = ""
    sequence_no: int = 0
    boundary: str = ""
    created_at: float = 0.0
    checkpoint_count: int = 0
    latest: bool = False
>>>>>>> REPLACE

File: `agintor/schemas.py`
<<<<<<< SEARCH
class CheckpointEnvelope(BaseModel):
    checkpoint_id: str
    runtime_abi: str
    storage_schema_version: str
    runtime_hash: str
    request_id: str
    plan_id: str
    task_id: str
    seed: int
    sequence_no: int = 0
    boundary: str = ""
    created_at: float = 0.0
    plan_snapshot: Dict[str, Any] = Field(default_factory=dict)
    task_payload: Dict[str, Any] = Field(default_factory=dict)
    queued_frames: List[QueuedFrameSnapshot] = Field(default_factory=list)
    plan_node_status: Dict[str, str] = Field(default_factory=dict)
    branch_state: List[BranchState] = Field(default_factory=list)
    branch_publications: List[BranchPublication] = Field(default_factory=list)
    unresolved_goals: List[str] = Field(default_factory=list)
    artifact_refs: Dict[str, Any] = Field(default_factory=dict)
    open_handle_snapshots: List[AsyncHandle] = Field(default_factory=list)
    handle_or_job_refs: List[str] = Field(default_factory=list)
    budget_state: Dict[str, Any] = Field(default_factory=dict)
    verifier_state: Dict[str, Any] = Field(default_factory=dict)
    working_state_summary: Dict[str, Any] = Field(default_factory=dict)
    trace_cursor: Dict[str, Any] = Field(default_factory=dict)
    side_effect_receipts: List[SideEffectReceipt] = Field(default_factory=list)
=======
class CheckpointEnvelope(BaseModel):
    checkpoint_id: str
    runtime_abi: str
    storage_schema_version: str
    runtime_hash: str
    run_id: str = ""
    run_root: str = ""
    attempt_id: str = ""
    request_id: str
    plan_id: str
    task_id: str
    seed: int
    sequence_no: int = 0
    boundary: str = ""
    created_at: float = 0.0
    plan_snapshot: Dict[str, Any] = Field(default_factory=dict)
    task_payload: Dict[str, Any] = Field(default_factory=dict)
    queued_frames: List[QueuedFrameSnapshot] = Field(default_factory=list)
    plan_node_status: Dict[str, str] = Field(default_factory=dict)
    branch_state: List[BranchState] = Field(default_factory=list)
    branch_publications: List[BranchPublication] = Field(default_factory=list)
    unresolved_goals: List[str] = Field(default_factory=list)
    artifact_refs: Dict[str, Any] = Field(default_factory=dict)
    open_handle_snapshots: List[AsyncHandle] = Field(default_factory=list)
    handle_or_job_refs: List[str] = Field(default_factory=list)
    budget_state: Dict[str, Any] = Field(default_factory=dict)
    verifier_state: Dict[str, Any] = Field(default_factory=dict)
    working_state_summary: Dict[str, Any] = Field(default_factory=dict)
    trace_cursor: Dict[str, Any] = Field(default_factory=dict)
    side_effect_receipts: List[SideEffectReceipt] = Field(default_factory=list)
>>>>>>> REPLACE

File: `agintor/schemas.py`
<<<<<<< SEARCH
class ResumeRequest(BaseModel):
    request_id: str
    checkpoint_ref: Optional[str] = None
    trace_context: Optional[OpenAITraceContext] = None
    reconciliation_policy: Literal["strict", "best_effort"] = "strict"


class RuntimeResumeRequest(BaseModel):
    request_id: str
    checkpoint_ref: str
    checkpoint_store_dir: str = ""
    trace_context: Optional[OpenAITraceContext] = None
    reconciliation_policy: Literal["strict", "best_effort"] = "strict"
=======
class ResumeRequest(BaseModel):
    request_id: str = ""
    run_ref: Optional[str] = None
    checkpoint_ref: Optional[str] = None
    trace_context: Optional[OpenAITraceContext] = None
    reconciliation_policy: Literal["strict", "best_effort"] = "strict"

    @root_validator(pre=False, allow_reuse=True)
    def validate_resume_target(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        run_ref = str(values.get("run_ref") or "").strip()
        checkpoint_ref = str(values.get("checkpoint_ref") or "").strip()
        if not run_ref and not checkpoint_ref:
            raise ValueError("resume requires run_ref or checkpoint_ref")
        return values


class RuntimeResumeRequest(BaseModel):
    request_id: str = ""
    run_ref: Optional[str] = None
    checkpoint_ref: Optional[str] = None
    run_id: str = ""
    run_root: str = ""
    attempt_id: str = ""
    checkpoint_store_dir: str = ""
    trace_context: Optional[OpenAITraceContext] = None
    reconciliation_policy: Literal["strict", "best_effort"] = "strict"

    @root_validator(pre=False, allow_reuse=True)
    def validate_resume_target(cls, values: Dict[str, Any]) -> Dict[str, Any]:
        run_ref = str(values.get("run_ref") or "").strip()
        checkpoint_ref = str(values.get("checkpoint_ref") or "").strip()
        if not run_ref and not checkpoint_ref:
            raise ValueError("resume requires run_ref or checkpoint_ref")
        return values
>>>>>>> REPLACE

File: `agintor/schemas.py`
<<<<<<< SEARCH
class RuntimeTaskInvocation(BaseModel):
    request_id: str
    seed: int
    task: BenchmarkTask
    trace_context: Optional[OpenAITraceContext] = None
=======
class RuntimeTaskInvocation(BaseModel):
    request_id: str
    run_id: str = ""
    run_root: str = ""
    attempt_id: str = ""
    seed: int
    task: BenchmarkTask
    trace_context: Optional[OpenAITraceContext] = None
>>>>>>> REPLACE

## `agintor/run_store.py`

Comment
- New helper owns run-root creation, manifest I/O, attempt numbering, checkpoint indexing, latest-checkpoint resolution, request/runtime bundle persistence, and prune rules.
- The helper intentionally keeps `run_manifest.json` and `request/` even after prune so `run_root` remains a durable audit anchor and the `pruned` lifecycle is observable instead of silently deleting the whole lineage.

File: `agintor/run_store.py`
<<<<<<< SEARCH
=======
from __future__ import annotations

import json
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .pydantic_compat import model_dump, model_validate
from .schemas import AttemptManifest, CheckpointEnvelope, CheckpointReference, RunManifest
from .utils import ensure_directory, now_ts, stable_hash


def _write_json_atomic(path: Path, payload: Any) -> None:
    ensure_directory(path.parent)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False, dir=str(path.parent)) as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        temp_path = Path(handle.name)
    temp_path.replace(path)


@dataclass(frozen=True)
class ResumeTarget:
    run_manifest: RunManifest
    checkpoint_path: Path


class RunStore:
    def __init__(self, workspace: str | Path, *, run_root: str | Path | None = None) -> None:
        self.workspace = Path(workspace)
        self.runs_root = ensure_directory(self.workspace / "runs")
        self.run_root = Path(run_root).resolve() if run_root is not None else None

    @classmethod
    def from_run_root(cls, run_root: str | Path) -> "RunStore":
        resolved = Path(run_root).resolve()
        return cls(resolved.parent.parent, run_root=resolved)

    def create_run(
        self,
        *,
        request_id: str,
        request_mode: str,
        runtime_backend: str,
        trace_context: Mapping[str, Any] | None,
        task_id: str | None = None,
        seed: int | None = None,
    ) -> RunManifest:
        created_at = now_ts()
        run_id = f"run.{int(created_at * 1000):013d}.{stable_hash(request_id, request_mode, created_at)[:12]}"
        run_root = ensure_directory(self.runs_root / run_id)
        ensure_directory(run_root / "request")
        ensure_directory(run_root / "attempts")
        ensure_directory(run_root / "checkpoints")
        ensure_directory(run_root / "traces")
        ensure_directory(run_root / "events")
        ensure_directory(run_root / "artifacts")
        ensure_directory(run_root / "side_effects")
        manifest = RunManifest(
            run_id=run_id,
            run_root=str(run_root),
            request_id=request_id,
            request_mode=request_mode,
            runtime_backend=runtime_backend,
            task_id=task_id,
            seed=seed,
            trace_context=trace_context,
            lifecycle_state="running",
            resumable=False,
            prune_eligible=False,
            created_at=created_at,
            updated_at=created_at,
        )
        self.write_run_manifest(manifest)
        return manifest

    def write_run_manifest(self, manifest: RunManifest) -> RunManifest:
        _write_json_atomic(self._require_run_root(manifest.run_root) / "run_manifest.json", model_dump(manifest))
        return manifest

    def load_run_manifest(self, run_ref: str | Path) -> RunManifest:
        run_root = self.resolve_run_root(run_ref)
        payload = json.loads((run_root / "run_manifest.json").read_text(encoding="utf-8"))
        return model_validate(RunManifest, payload)

    def resolve_run_root(self, run_ref: str | Path) -> Path:
        candidate = Path(str(run_ref)).expanduser()
        if candidate.exists():
            resolved = candidate.resolve()
            if resolved.is_file():
                resolved = resolved.parent
            if (resolved / "run_manifest.json").exists():
                return resolved
        resolved = (self.runs_root / str(run_ref).strip()).resolve()
        if not (resolved / "run_manifest.json").exists():
            raise FileNotFoundError(f"unknown run_ref: {run_ref}")
        return resolved

    def begin_attempt(
        self,
        manifest: RunManifest,
        *,
        launch_kind: str,
        resumed_from_checkpoint_ref: str | None = None,
    ) -> AttemptManifest:
        run_root = self._require_run_root(manifest.run_root)
        attempts_dir = ensure_directory(run_root / "attempts")
        existing = sorted(path for path in attempts_dir.glob("attempt_*") if path.is_dir())
        sequence_no = len(existing) + 1
        attempt_id = f"attempt_{sequence_no:04d}"
        attempt_dir = ensure_directory(attempts_dir / attempt_id)
        workspace_root = ensure_directory(attempt_dir / "workspace")
        created_at = now_ts()
        attempt = AttemptManifest(
            attempt_id=attempt_id,
            run_id=manifest.run_id,
            run_root=manifest.run_root,
            sequence_no=sequence_no,
            launch_kind=launch_kind,
            resumed_from_checkpoint_ref=resumed_from_checkpoint_ref,
            workspace_root=str(workspace_root),
            started_at=created_at,
            updated_at=created_at,
        )
        _write_json_atomic(attempt_dir / "attempt_manifest.json", model_dump(attempt))
        updated = manifest.copy(update={"current_attempt_id": attempt_id, "updated_at": created_at})
        self.write_run_manifest(updated)
        return attempt

    def finish_attempt(
        self,
        attempt: AttemptManifest,
        *,
        lifecycle_state: str,
        latest_checkpoint_ref: str | None = None,
        failure_kind: str | None = None,
    ) -> AttemptManifest:
        updated = attempt.copy(
            update={
                "lifecycle_state": lifecycle_state,
                "latest_checkpoint_ref": latest_checkpoint_ref,
                "failure_kind": failure_kind,
                "updated_at": now_ts(),
                "finished_at": now_ts(),
            }
        )
        attempt_dir = self._require_run_root(updated.run_root) / "attempts" / updated.attempt_id
        _write_json_atomic(attempt_dir / "attempt_manifest.json", model_dump(updated))
        return updated

    def write_request_bundle(
        self,
        manifest: RunManifest,
        *,
        request_kind: str,
        request_payload: Mapping[str, Any],
        plan_payload: Mapping[str, Any] | None = None,
        task_payload: Mapping[str, Any] | None = None,
        runtime_identity: Mapping[str, Any] | None = None,
    ) -> None:
        request_dir = ensure_directory(self._require_run_root(manifest.run_root) / "request")
        _write_json_atomic(request_dir / "request.json", {"request_kind": request_kind, "payload": dict(request_payload)})
        if plan_payload is not None:
            _write_json_atomic(request_dir / "plan.json", dict(plan_payload))
        if task_payload is not None:
            _write_json_atomic(request_dir / "task.json", dict(task_payload))
        if runtime_identity is not None:
            _write_json_atomic(request_dir / "runtime_identity.json", dict(runtime_identity))

    def load_request_bundle(self, manifest: RunManifest) -> Mapping[str, Any]:
        request_dir = self._require_run_root(manifest.run_root) / "request"
        return json.loads((request_dir / "request.json").read_text(encoding="utf-8"))

    def write_checkpoint(self, envelope: CheckpointEnvelope) -> CheckpointReference:
        run_root = self._require_run_root(envelope.run_root)
        checkpoints_dir = ensure_directory(run_root / "checkpoints")
        checkpoint_path = checkpoints_dir / f"{envelope.checkpoint_id}.json"
        _write_json_atomic(checkpoint_path, model_dump(envelope))
        index_path = checkpoints_dir / "index.json"
        index_rows = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else []
        checkpoint_ref = CheckpointReference(
            ref=str(checkpoint_path.resolve()),
            run_id=envelope.run_id,
            run_root=envelope.run_root,
            attempt_id=envelope.attempt_id,
            task_id=envelope.task_id,
            seed=envelope.seed,
            request_id=envelope.request_id,
            plan_id=envelope.plan_id,
            checkpoint_id=envelope.checkpoint_id,
            sequence_no=envelope.sequence_no,
            boundary=envelope.boundary,
            created_at=envelope.created_at,
            checkpoint_count=len(index_rows) + 1,
            latest=True,
        )
        index_rows = [row for row in index_rows if row.get("checkpoint_id") != envelope.checkpoint_id]
        index_rows.append(model_dump(checkpoint_ref))
        index_rows.sort(key=lambda row: (int(row.get("sequence_no", 0) or 0), float(row.get("created_at", 0.0) or 0.0)))
        _write_json_atomic(index_path, index_rows)
        _write_json_atomic(checkpoints_dir / "LATEST.json", model_dump(checkpoint_ref))
        manifest = self.load_run_manifest(envelope.run_root)
        self.write_run_manifest(
            manifest.copy(
                update={
                    "current_attempt_id": envelope.attempt_id,
                    "latest_checkpoint_ref": checkpoint_ref.ref,
                    "resumable": True,
                    "updated_at": now_ts(),
                }
            )
        )
        return checkpoint_ref

    def latest_checkpoint_ref(self, run_ref: str | Path) -> str | None:
        checkpoints_dir = self.resolve_run_root(run_ref) / "checkpoints"
        latest_path = checkpoints_dir / "LATEST.json"
        if not latest_path.exists():
            return None
        payload = json.loads(latest_path.read_text(encoding="utf-8"))
        return str(payload.get("ref") or payload.get("checkpoint_ref") or "").strip() or None

    def resolve_resume_target(
        self,
        *,
        run_ref: str | Path | None = None,
        checkpoint_ref: str | Path | None = None,
    ) -> ResumeTarget:
        if checkpoint_ref:
            checkpoint_path = Path(str(checkpoint_ref)).expanduser().resolve()
            run_root = self._find_run_root_for_checkpoint(checkpoint_path)
            return ResumeTarget(self.load_run_manifest(run_root), checkpoint_path)
        if not run_ref:
            raise FileNotFoundError("resume requires run_ref or checkpoint_ref")
        manifest = self.load_run_manifest(run_ref)
        latest = self.latest_checkpoint_ref(manifest.run_root)
        if not latest:
            raise FileNotFoundError(f"run {manifest.run_id} has no latest checkpoint")
        return ResumeTarget(manifest, Path(latest).resolve())

    def finish_run(
        self,
        manifest: RunManifest,
        *,
        lifecycle_state: str,
        latest_checkpoint_ref: str | None,
        resumable: bool,
        failure_kind: str | None = None,
    ) -> RunManifest:
        updated = manifest.copy(
            update={
                "lifecycle_state": lifecycle_state,
                "latest_checkpoint_ref": latest_checkpoint_ref,
                "resumable": resumable,
                "prune_eligible": lifecycle_state == "failed" and not resumable,
                "last_failure_kind": failure_kind,
                "updated_at": now_ts(),
            }
        )
        self.write_run_manifest(updated)
        return updated

    def prune_run(self, manifest: RunManifest) -> RunManifest:
        run_root = self._require_run_root(manifest.run_root)
        for name in ("checkpoints", "traces", "events", "artifacts", "side_effects"):
            shutil.rmtree(run_root / name, ignore_errors=True)
        for attempt_dir in (run_root / "attempts").glob("attempt_*"):
            shutil.rmtree(attempt_dir / "workspace", ignore_errors=True)
        updated = manifest.copy(
            update={
                "lifecycle_state": "pruned",
                "latest_checkpoint_ref": None,
                "resumable": False,
                "prune_eligible": False,
                "updated_at": now_ts(),
            }
        )
        self.write_run_manifest(updated)
        return updated

    def _find_run_root_for_checkpoint(self, checkpoint_path: Path) -> Path:
        for parent in checkpoint_path.parents:
            if (parent / "run_manifest.json").exists():
                return parent
        raise FileNotFoundError(f"checkpoint {checkpoint_path} is not inside a durable run root")

    @staticmethod
    def _require_run_root(run_root: str | Path) -> Path:
        resolved = Path(run_root).expanduser().resolve()
        ensure_directory(resolved)
        return resolved
>>>>>>> REPLACE

## `agintor/runtime_host.py`

Comment
- Create a durable run root before launching solve or resume.
- Remove global request-id checkpoint discovery from the primary contract.
- Make resume load the original stored request bundle and reuse the same solve preflight path.
- Finalize run/attempt manifests from the runtime response instead of blanking paths on success.

File: `agintor/runtime_host.py`
<<<<<<< SEARCH
from .runtime_api import inspect_request_for_runtime, runtime_batch_request_for_tasks, solve_request_to_task
=======
from .run_store import RunStore
from .runtime_api import batch_evaluation_unit_key, inspect_request_for_runtime, runtime_batch_request_for_tasks, solve_request_to_task
>>>>>>> REPLACE

File: `agintor/runtime_host.py`
<<<<<<< SEARCH
        self.container_executor = (
            DockerRuntimeExecutor(
                self.workspace / ".runtime_host",
                artifact_mode=self.artifact_policy.mode,
                sandbox_root=self.artifact_policy.sandbox_root,
            )
            if self.runtime_backend == "docker"
            else None
        )
=======
        self.run_store = RunStore(self.workspace)
        self.container_executor = (
            DockerRuntimeExecutor(
                self.workspace / ".runtime_host",
                artifact_mode=self.artifact_policy.mode,
                sandbox_root=self.artifact_policy.sandbox_root,
            )
            if self.runtime_backend == "docker"
            else None
        )
>>>>>>> REPLACE

File: `agintor/runtime_host.py`
<<<<<<< SEARCH
    def solve(
        self,
        runtime_dir: str | Path,
        request: RuntimeSolveRequest,
        *,
        provider: ModelProvider,
        runtime_profile: object | None = None,
    ) -> RuntimeSolveResponse:
        capability_exchange = self.inspect(runtime_dir)
        self._preflight_runtime_guarantees(runtime_dir, capability_exchange)
        self._preflight_solve_contract(
            runtime_dir,
            capability_exchange,
            request,
            provider=provider,
            runtime_profile=runtime_profile,
        )
        if self.runtime_backend == "docker" and self.container_executor is not None:
            response = self.container_executor.solve_protocol(
                runtime_dir,
                request,
                provider=provider,
                runtime_profile=runtime_profile,
            )
        else:
            response = self._run_local_solve(
                Path(runtime_dir),
                request,
                provider=provider,
                runtime_profile=runtime_profile,
            )
        if response.capability_exchange != capability_exchange:
            raise RuntimeLoadError("runtime capability exchange changed between inspect and solve")
        failed = response.solve_result.status in {"failed", "controlled_failure"} or bool(response.solve_result.faults.get("hard_invalid"))
        self._prune_solve_result_artifacts(response, failed=failed)
        return response
=======
    def solve(
        self,
        runtime_dir: str | Path,
        request: RuntimeSolveRequest,
        *,
        provider: ModelProvider,
        runtime_profile: object | None = None,
    ) -> RuntimeSolveResponse:
        capability_exchange = self.inspect(runtime_dir)
        run_manifest = self.run_store.create_run(
            request_id=request.request_id,
            request_mode=request.mode,
            runtime_backend=self.runtime_backend,
            trace_context=model_dump(request.trace_context) if request.trace_context is not None else None,
            task_id=request.task.task_id if request.task is not None else None,
            seed=request.seed,
        )
        attempt_manifest = self.run_store.begin_attempt(run_manifest, launch_kind="solve")
        runtime_request = request.copy(
            update={
                "run_id": run_manifest.run_id,
                "run_root": run_manifest.run_root,
                "attempt_id": attempt_manifest.attempt_id,
            }
        )
        try:
            self._preflight_runtime_guarantees(runtime_dir, capability_exchange)
            self._preflight_solve_contract(
                runtime_dir,
                capability_exchange,
                runtime_request,
                provider=provider,
                runtime_profile=runtime_profile,
            )
            if self.runtime_backend == "docker" and self.container_executor is not None:
                response = self.container_executor.solve_protocol(
                    runtime_dir,
                    runtime_request,
                    provider=provider,
                    runtime_profile=runtime_profile,
                )
            else:
                response = self._run_local_solve(
                    Path(runtime_dir),
                    runtime_request,
                    provider=provider,
                    runtime_profile=runtime_profile,
                )
        except Exception:
            self.run_store.finish_attempt(attempt_manifest, lifecycle_state="crashed")
            updated = self.run_store.finish_run(
                run_manifest,
                lifecycle_state="failed",
                latest_checkpoint_ref=None,
                resumable=False,
                failure_kind="host_launch_failure",
            )
            if updated.prune_eligible and not self.artifact_policy.keep_failures:
                self.run_store.prune_run(updated)
            raise
        if response.capability_exchange != capability_exchange:
            raise RuntimeLoadError("runtime capability exchange changed between inspect and solve")
        self._finalize_durable_run(
            run_manifest,
            attempt_manifest,
            response,
            failure_kind=response.solve_result.faults.get("code") if isinstance(response.solve_result.faults, dict) else None,
        )
        return response
>>>>>>> REPLACE

File: `agintor/runtime_host.py`
<<<<<<< SEARCH
    def run_batch(
        self,
        runtime_dir: str | Path,
        task_runs: list[tuple[object, int]],
        *,
        provider: ModelProvider,
        runtime_profile: object | None = None,
        budget_overrides: Mapping[str, Any] | None = None,
    ) -> RuntimeBatchResponse:
        capability_exchange = self.inspect(runtime_dir)
        self._preflight_runtime_guarantees(runtime_dir, capability_exchange)
        request = runtime_batch_request_for_tasks(
            request_id=f"run.{stable_hash(runtime_dir, len(task_runs), self.runtime_backend)[:12]}",
            runtime_backend=self.runtime_backend,
            task_runs=task_runs,
            budget_overrides=dict(budget_overrides or {}),
        )
        if self.runtime_backend == "docker" and self.container_executor is not None:
            response = self.container_executor.run_batch_protocol(
                runtime_dir,
                request,
                provider=provider,
                runtime_profile=runtime_profile,
            )
        else:
            response = self._run_local_batch(
                Path(runtime_dir),
                request,
                provider=provider,
                runtime_profile=runtime_profile,
            )
        if response.capability_exchange != capability_exchange:
            raise RuntimeLoadError("runtime capability exchange changed between inspect and execution")
        failed = any(run.hard_invalid for run in response.run_results)
        if not self._should_retain_run_dir(failed=failed):
            for run in response.run_results:
                run.trace_path = None
                run.checkpoint_ref = None
        return response
=======
    def run_batch(
        self,
        runtime_dir: str | Path,
        task_runs: list[tuple[object, int]],
        *,
        provider: ModelProvider,
        runtime_profile: object | None = None,
        budget_overrides: Mapping[str, Any] | None = None,
    ) -> RuntimeBatchResponse:
        capability_exchange = self.inspect(runtime_dir)
        self._preflight_runtime_guarantees(runtime_dir, capability_exchange)
        request = runtime_batch_request_for_tasks(
            request_id=f"run.{stable_hash(runtime_dir, len(task_runs), self.runtime_backend)[:12]}",
            runtime_backend=self.runtime_backend,
            task_runs=task_runs,
            budget_overrides=dict(budget_overrides or {}),
        )
        group_contexts: dict[str, tuple[RunManifest, AttemptManifest]] = {}
        for invocation in request.invocations:
            group_key = batch_evaluation_unit_key(invocation)
            if group_key not in group_contexts:
                manifest = self.run_store.create_run(
                    request_id=invocation.request_id,
                    request_mode="batch",
                    runtime_backend=self.runtime_backend,
                    trace_context=model_dump(invocation.trace_context) if invocation.trace_context is not None else None,
                    task_id=invocation.task.task_id,
                    seed=invocation.seed,
                )
                attempt = self.run_store.begin_attempt(manifest, launch_kind="run_batch")
                group_contexts[group_key] = (manifest, attempt)
            manifest, attempt = group_contexts[group_key]
            invocation.run_id = manifest.run_id
            invocation.run_root = manifest.run_root
            invocation.attempt_id = attempt.attempt_id
        if self.runtime_backend == "docker" and self.container_executor is not None:
            response = self.container_executor.run_batch_protocol(
                runtime_dir,
                request,
                provider=provider,
                runtime_profile=runtime_profile,
            )
        else:
            response = self._run_local_batch(
                Path(runtime_dir),
                request,
                provider=provider,
                runtime_profile=runtime_profile,
            )
        if response.capability_exchange != capability_exchange:
            raise RuntimeLoadError("runtime capability exchange changed between inspect and execution")
        for group_key, (manifest, attempt) in group_contexts.items():
            grouped_runs = [run for run in response.run_results if run.run_id == manifest.run_id]
            if not grouped_runs:
                self.run_store.finish_attempt(attempt, lifecycle_state="crashed")
                updated = self.run_store.finish_run(
                    manifest,
                    lifecycle_state="failed",
                    latest_checkpoint_ref=None,
                    resumable=False,
                    failure_kind="missing_batch_result",
                )
                if updated.prune_eligible and not self.artifact_policy.keep_failures:
                    self.run_store.prune_run(updated)
                continue
            terminal = grouped_runs[-1]
            latest_checkpoint_ref = terminal.latest_checkpoint_ref or terminal.checkpoint_ref
            failed = bool(terminal.hard_invalid)
            attempt_state = "failed" if failed and not latest_checkpoint_ref else ("paused" if failed else "completed")
            run_state = "failed" if failed and not latest_checkpoint_ref else ("paused" if failed else "completed")
            self.run_store.finish_attempt(
                attempt,
                lifecycle_state=attempt_state,
                latest_checkpoint_ref=latest_checkpoint_ref,
                failure_kind=terminal.failure_kind,
            )
            updated = self.run_store.finish_run(
                manifest,
                lifecycle_state=run_state,
                latest_checkpoint_ref=latest_checkpoint_ref,
                resumable=bool(latest_checkpoint_ref),
                failure_kind=terminal.failure_kind,
            )
            if updated.prune_eligible and not self.artifact_policy.keep_failures:
                updated = self.run_store.prune_run(updated)
            for run in grouped_runs:
                run.run_id = updated.run_id
                run.run_root = updated.run_root
                run.attempt_id = attempt.attempt_id
                run.latest_checkpoint_ref = latest_checkpoint_ref if updated.lifecycle_state != "pruned" else None
                run.run_lifecycle_state = updated.lifecycle_state
                run.run_resumable = updated.resumable
                run.run_prune_eligible = updated.prune_eligible
        return response
>>>>>>> REPLACE

File: `agintor/runtime_host.py`
<<<<<<< SEARCH
    def resume(
        self,
        runtime_dir: str | Path,
        request: ResumeRequest,
        *,
        provider: ModelProvider,
        runtime_profile: object | None = None,
    ) -> RuntimeSolveResponse:
        capability_exchange = self.inspect(runtime_dir)
        self._preflight_runtime_guarantees(runtime_dir, capability_exchange)
        if not capability_exchange.resume_support:
            raise RuntimeLoadError(f"runtime {runtime_dir} does not advertise resume support")
        runtime_request = self._resolve_runtime_resume_request(request)
        if self.runtime_backend == "docker" and self.container_executor is not None:
            response = self.container_executor.resume_protocol(
                runtime_dir,
                runtime_request,
                provider=provider,
                runtime_profile=runtime_profile,
            )
        else:
            response = self._run_local_resume(
                Path(runtime_dir),
                runtime_request,
                provider=provider,
                runtime_profile=runtime_profile,
            )
        if response.capability_exchange != capability_exchange:
            raise RuntimeLoadError("runtime capability exchange changed between inspect and resume")
        failed = response.solve_result.status in {"failed", "controlled_failure"} or bool(response.solve_result.faults.get("hard_invalid"))
        self._prune_solve_result_artifacts(response, failed=failed)
        return response
=======
    def resume(
        self,
        runtime_dir: str | Path,
        request: ResumeRequest,
        *,
        provider: ModelProvider,
        runtime_profile: object | None = None,
    ) -> RuntimeSolveResponse:
        capability_exchange = self.inspect(runtime_dir)
        if not capability_exchange.resume_support:
            raise RuntimeLoadError(f"runtime {runtime_dir} does not advertise resume support")
        runtime_request, original_request, run_manifest, attempt_manifest = self._resolve_runtime_resume_request(request)
        try:
            self._preflight_runtime_guarantees(runtime_dir, capability_exchange)
            self._preflight_solve_contract(
                runtime_dir,
                capability_exchange,
                original_request,
                provider=provider,
                runtime_profile=runtime_profile,
            )
            if self.runtime_backend == "docker" and self.container_executor is not None:
                response = self.container_executor.resume_protocol(
                    runtime_dir,
                    runtime_request,
                    provider=provider,
                    runtime_profile=runtime_profile,
                )
            else:
                response = self._run_local_resume(
                    Path(runtime_dir),
                    runtime_request,
                    provider=provider,
                    runtime_profile=runtime_profile,
                )
        except Exception:
            self.run_store.finish_attempt(attempt_manifest, lifecycle_state="crashed")
            updated = self.run_store.finish_run(
                run_manifest,
                lifecycle_state="failed",
                latest_checkpoint_ref=run_manifest.latest_checkpoint_ref,
                resumable=bool(run_manifest.latest_checkpoint_ref),
                failure_kind="host_launch_failure",
            )
            if updated.prune_eligible and not self.artifact_policy.keep_failures:
                self.run_store.prune_run(updated)
            raise
        if response.capability_exchange != capability_exchange:
            raise RuntimeLoadError("runtime capability exchange changed between inspect and resume")
        self._finalize_durable_run(
            run_manifest,
            attempt_manifest,
            response,
            failure_kind=response.solve_result.faults.get("code") if isinstance(response.solve_result.faults, dict) else None,
        )
        return response
>>>>>>> REPLACE

File: `agintor/runtime_host.py`
<<<<<<< SEARCH
    def _resolve_runtime_resume_request(self, request: ResumeRequest) -> RuntimeResumeRequest:
        checkpoint_path, checkpoint_store_dir = self._resolve_resume_checkpoint(request)
        return RuntimeResumeRequest(
            request_id=request.request_id,
            checkpoint_ref=str(checkpoint_path),
            checkpoint_store_dir=str(checkpoint_store_dir),
            trace_context=request.trace_context,
            reconciliation_policy=request.reconciliation_policy,
        )

    def _resolve_resume_checkpoint(self, request: ResumeRequest) -> tuple[Path, Path]:
        checkpoint_ref = str(request.checkpoint_ref or "").strip()
        if checkpoint_ref:
            checkpoint_path = Path(checkpoint_ref).expanduser().resolve()
            if not checkpoint_path.exists():
                raise RuntimeLoadError(f"resume checkpoint does not exist: {checkpoint_path}")
            return checkpoint_path, self._checkpoint_store_root_for_ref(checkpoint_path, request.request_id)
        discovered = self._discover_latest_checkpoint(request.request_id)
        if discovered is None:
            raise RuntimeLoadError(f"no latest checkpoint published for request {request.request_id}")
        return discovered

    def _discover_latest_checkpoint(self, request_id: str) -> tuple[Path, Path] | None:
        best: tuple[float, int, str, Path, Path] | None = None
        for latest_path in self.workspace.rglob("LATEST.json"):
            request_dir = latest_path.parent
            if request_dir.name != request_id or request_dir.parent.name != "checkpoints":
                continue
            try:
                payload = json.loads(latest_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            checkpoint_path = self._checkpoint_path_from_record(request_dir, payload)
            if checkpoint_path is None or not checkpoint_path.exists():
                continue
            created_at = float(payload.get("created_at", 0.0) or 0.0)
            sequence_no = int(payload.get("sequence_no", 0) or 0)
            candidate = (created_at, sequence_no, str(checkpoint_path), checkpoint_path, request_dir.parent)
            if best is None or candidate[:3] > best[:3]:
                best = candidate
        if best is None:
            return None
        return best[3].resolve(), best[4].resolve()
=======
    def _resolve_runtime_resume_request(
        self,
        request: ResumeRequest,
    ) -> tuple[RuntimeResumeRequest, RuntimeSolveRequest, RunManifest, AttemptManifest]:
        target = self.run_store.resolve_resume_target(run_ref=request.run_ref, checkpoint_ref=request.checkpoint_ref)
        bundle = self.run_store.load_request_bundle(target.run_manifest)
        if bundle.get("request_kind") != "runtime_solve_request":
            raise RuntimeLoadError(f"run {target.run_manifest.run_id} does not contain a resumable solve request bundle")
        original_request = model_validate(RuntimeSolveRequest, bundle.get("payload") or {})
        attempt_manifest = self.run_store.begin_attempt(
            target.run_manifest,
            launch_kind="resume",
            resumed_from_checkpoint_ref=str(target.checkpoint_path.resolve()),
        )
        runtime_request = RuntimeResumeRequest(
            request_id=str(request.request_id or original_request.request_id),
            run_ref=target.run_manifest.run_id,
            checkpoint_ref=str(target.checkpoint_path.resolve()),
            run_id=target.run_manifest.run_id,
            run_root=target.run_manifest.run_root,
            attempt_id=attempt_manifest.attempt_id,
            checkpoint_store_dir=str(Path(target.run_manifest.run_root) / "checkpoints"),
            trace_context=request.trace_context or original_request.trace_context,
            reconciliation_policy=request.reconciliation_policy,
        )
        return runtime_request, original_request, target.run_manifest, attempt_manifest
>>>>>>> REPLACE

Comment
- Delete `_resolve_resume_checkpoint(...)`, `_discover_latest_checkpoint(...)`, and `_checkpoint_store_root_for_ref(...)` entirely after the `RunStore` resolver above lands. The primary contract must stop scanning the whole workspace for matching `request_id`.

File: `agintor/runtime_host.py`
<<<<<<< SEARCH
    def _prune_solve_result_artifacts(self, response: RuntimeSolveResponse, *, failed: bool) -> None:
        if self._should_retain_run_dir(failed=failed):
            return
        if response.solve_result.trace_ref and not self._is_inline_trace_ref(response.solve_result.trace_ref):
            response.solve_result.trace_ref = None
        response.solve_result.checkpoint_ref = None
        response.solve_result.recoverability = self._recoverability_without_checkpoint(response.solve_result.status)
=======
    def _finalize_durable_run(
        self,
        run_manifest: RunManifest,
        attempt_manifest: AttemptManifest,
        response: RuntimeSolveResponse,
        *,
        failure_kind: str | None,
    ) -> None:
        latest_checkpoint_ref = response.solve_result.latest_checkpoint_ref or response.solve_result.checkpoint_ref
        failed = response.solve_result.status in {"failed", "controlled_failure"} or bool(response.solve_result.faults.get("hard_invalid"))
        resumable = bool(latest_checkpoint_ref)
        run_state = "completed"
        attempt_state = "completed"
        if failed and resumable:
            run_state = "paused"
            attempt_state = "paused"
        elif failed:
            run_state = "failed"
            attempt_state = "failed"
        finished_attempt = self.run_store.finish_attempt(
            attempt_manifest,
            lifecycle_state=attempt_state,
            latest_checkpoint_ref=latest_checkpoint_ref,
            failure_kind=failure_kind,
        )
        finished_run = self.run_store.finish_run(
            run_manifest,
            lifecycle_state=run_state,
            latest_checkpoint_ref=latest_checkpoint_ref,
            resumable=resumable,
            failure_kind=failure_kind,
        )
        if finished_run.prune_eligible and not self.artifact_policy.keep_failures:
            finished_run = self.run_store.prune_run(finished_run)
        response.solve_result.run_id = finished_run.run_id
        response.solve_result.run_root = finished_run.run_root
        response.solve_result.attempt_id = finished_attempt.attempt_id
        response.solve_result.latest_checkpoint_ref = latest_checkpoint_ref if finished_run.lifecycle_state != "pruned" else None
        response.solve_result.run_lifecycle_state = finished_run.lifecycle_state
        response.solve_result.run_resumable = finished_run.resumable
        response.solve_result.run_prune_eligible = finished_run.prune_eligible
        response.solve_result.recoverability = (
            "checkpoint_available"
            if response.solve_result.latest_checkpoint_ref
            else self._recoverability_without_checkpoint(response.solve_result.status)
        )
>>>>>>> REPLACE

Comment
- Keep `_cleanup_run_dir(...)` only for inspect scratch and any non-authoritative transport scratch. Do not use it to delete durable run roots created under `workspace/runs/<run_id>/`.

## `agintor/runtime_sdk/runtime_entry.py`

Comment
- Use the durable run root and attempt workspace passed over transport.
- Persist `request/request.json`, `request/plan.json`, `request/task.json`, and `request/runtime_identity.json` into the run root.
- Resume should load the original stored request bundle so user-request resumes keep the original prompt/request contract instead of reconstructing a fake benchmark solve request.

File: `agintor/runtime_sdk/runtime_entry.py`
<<<<<<< SEARCH
from .runner import TaskRuntime
=======
from .run_store import RunStore
from .runner import TaskRuntime
>>>>>>> REPLACE

File: `agintor/runtime_sdk/runtime_entry.py`
<<<<<<< SEARCH
        if runner is None:
            shell = FixedShell(
                Path(args.workspace) / group_key.replace("/", "_"),
                artifact_mode=ArtifactMode(args.artifact_mode),
            )
            runner = TaskRuntime(
                runtime,
                shell,
                provider,
                budget_overrides=request.budget_overrides,
                runtime_profile=runtime_profile,
            )
            runners_by_group[group_key] = runner
=======
        if runner is None:
            if not invocation.run_root or not invocation.attempt_id:
                raise ValueError("runtime batch invocations require durable run_root and attempt_id")
            run_store = RunStore.from_run_root(invocation.run_root)
            run_store.write_request_bundle(
                run_store.load_run_manifest(invocation.run_root),
                request_kind="runtime_task_invocation",
                request_payload=model_dump(invocation),
                runtime_identity={
                    "runtime_hash": runtime.runtime_hash,
                    "runtime_abi": runtime.kernel_manifest.runtime_abi,
                    "storage_schema_version": runtime.kernel_manifest.storage_schema_version,
                    "runtime_dir": str(runtime.runtime_dir),
                },
            )
            shell = FixedShell(
                Path(invocation.run_root) / "attempts" / invocation.attempt_id / "workspace",
                artifact_mode=ArtifactMode(args.artifact_mode),
                run_store=run_store,
                run_id=invocation.run_id,
                attempt_id=invocation.attempt_id,
            )
            runner = TaskRuntime(
                runtime,
                shell,
                provider,
                budget_overrides=request.budget_overrides,
                runtime_profile=runtime_profile,
            )
            runners_by_group[group_key] = runner
>>>>>>> REPLACE

Comment
- Worker 03 should add the ordered `(episode_order, task_id, request_id)` sort immediately above the loop and should ensure all invocations that share one `group_key` also share the same stamped `run_id` / `run_root` / `attempt_id`.

File: `agintor/runtime_sdk/runtime_entry.py`
<<<<<<< SEARCH
    provider = None
    try:
        provider = build_provider_from_payload(provider_payload_data, provider_profile=runtime_profile.runtime_provider)
        shell = FixedShell(
            Path(args.workspace) / f"seed_{request.seed}",
            artifact_mode=ArtifactMode(args.artifact_mode),
        )
        runner = TaskRuntime(
            runtime,
            shell,
            provider,
            budget_overrides=request.budget_overrides,
            runtime_profile=runtime_profile,
        )
=======
    if not request.run_root or not request.attempt_id:
        raise ValueError("solve requires durable run_root and attempt_id")
    run_store = RunStore.from_run_root(request.run_root)
    run_manifest = run_store.load_run_manifest(request.run_root)
    run_store.write_request_bundle(
        run_manifest,
        request_kind="runtime_solve_request",
        request_payload=model_dump(request),
        runtime_identity={
            "runtime_hash": runtime.runtime_hash,
            "runtime_abi": runtime.kernel_manifest.runtime_abi,
            "storage_schema_version": runtime.kernel_manifest.storage_schema_version,
            "runtime_dir": str(runtime.runtime_dir),
        },
    )
    provider = None
    try:
        provider = build_provider_from_payload(provider_payload_data, provider_profile=runtime_profile.runtime_provider)
        run_store.write_request_bundle(
            run_manifest,
            request_kind="runtime_solve_request",
            request_payload=model_dump(request),
            plan_payload=model_dump(execution_plan),
            task_payload=model_dump(task),
            runtime_identity={
                "runtime_hash": runtime.runtime_hash,
                "runtime_abi": runtime.kernel_manifest.runtime_abi,
                "storage_schema_version": runtime.kernel_manifest.storage_schema_version,
                "runtime_dir": str(runtime.runtime_dir),
            },
        )
        shell = FixedShell(
            Path(request.run_root) / "attempts" / request.attempt_id / "workspace",
            artifact_mode=ArtifactMode(args.artifact_mode),
            run_store=run_store,
            run_id=request.run_id,
            attempt_id=request.attempt_id,
        )
        runner = TaskRuntime(
            runtime,
            shell,
            provider,
            budget_overrides=request.budget_overrides,
            runtime_profile=runtime_profile,
        )
>>>>>>> REPLACE

File: `agintor/runtime_sdk/runtime_entry.py`
<<<<<<< SEARCH
        response = runtime_solve_failure_response(
            solve_request,
            runtime.runtime_hash,
            capability_exchange,
            mode=request.mode,
            summary=summary,
            provider_usage=provider.usage_summary() if provider is not None else {},
            fault_code=_solve_failure_code(exc),
        )
=======
        response = runtime_solve_failure_response(
            solve_request,
            runtime.runtime_hash,
            capability_exchange,
            mode=request.mode,
            summary=summary,
            provider_usage=provider.usage_summary() if provider is not None else {},
            fault_code=_solve_failure_code(exc),
            run_id=request.run_id,
            run_root=request.run_root,
            attempt_id=request.attempt_id,
        )
>>>>>>> REPLACE

File: `agintor/runtime_sdk/runtime_entry.py`
<<<<<<< SEARCH
    provider = build_provider_from_payload(provider_payload_data, provider_profile=runtime_profile.runtime_provider)
    shell = FixedShell(
        Path(args.workspace) / f"resume_{request.request_id}",
        artifact_mode=ArtifactMode(args.artifact_mode),
    )
    shell.configure_resume_checkpoint_store(request.checkpoint_store_dir)
    envelope = shell.load_checkpoint_envelope(
        checkpoint_ref=request.checkpoint_ref,
        request_id=request.request_id,
        checkpoint_store_dir=request.checkpoint_store_dir,
    )
    task = model_validate(BenchmarkTask, envelope.task_payload)
    solve_request = benchmark_task_to_solve_request(task, request_id=request.request_id)
    plan = model_validate(ExecutionPlan, envelope.plan_snapshot)
=======
    if not request.run_root or not request.attempt_id:
        raise ValueError("resume requires durable run_root and attempt_id")
    run_store = RunStore.from_run_root(request.run_root)
    run_manifest = run_store.load_run_manifest(request.run_root)
    bundle = run_store.load_request_bundle(run_manifest)
    provider = build_provider_from_payload(provider_payload_data, provider_profile=runtime_profile.runtime_provider)
    shell = FixedShell(
        Path(request.run_root) / "attempts" / request.attempt_id / "workspace",
        artifact_mode=ArtifactMode(args.artifact_mode),
        run_store=run_store,
        run_id=request.run_id or run_manifest.run_id,
        attempt_id=request.attempt_id,
    )
    envelope = shell.load_checkpoint_envelope(
        checkpoint_ref=request.checkpoint_ref,
        run_ref=request.run_ref or request.run_root,
        checkpoint_store_dir=request.checkpoint_store_dir,
    )
    task = model_validate(BenchmarkTask, envelope.task_payload)
    plan = model_validate(ExecutionPlan, envelope.plan_snapshot)
    if bundle.get("request_kind") == "runtime_solve_request":
        original_request = model_validate(RuntimeSolveRequest, bundle.get("payload") or {})
        if original_request.mode == "user_request" and original_request.solve_request is not None:
            solve_request = model_validate(SolveRequest, model_dump(original_request.solve_request))
        else:
            solve_request = benchmark_task_to_solve_request(task, request_id=original_request.request_id)
    else:
        solve_request = benchmark_task_to_solve_request(task, request_id=str(request.request_id or envelope.request_id))
>>>>>>> REPLACE

## `agintor/shell.py`

Comment
- `FixedShell` should write traces and checkpoints into the durable run root when one is attached.
- Keep fallback behavior only for unit tests or non-run-store call sites; the new primary path should be `RunStore`.

File: `agintor/shell.py`
<<<<<<< SEARCH
from .schemas import AgentTemplate, AsyncHandle, CheckpointEnvelope, CheckpointReference
=======
from .run_store import RunStore
from .schemas import AgentTemplate, AsyncHandle, CheckpointEnvelope, CheckpointReference
>>>>>>> REPLACE

File: `agintor/shell.py`
<<<<<<< SEARCH
class FixedShell:
    def __init__(
        self,
        workspace: Path,
        predictors: DecisionFamilyModelBank | None = None,
        *,
        artifact_mode: str | ArtifactMode | None = None,
        sandbox_root: Path | None = None,
    ) -> None:
        self.workspace = Path(workspace)
        self.artifact_policy = ArtifactPolicy.resolve(
            artifact_mode=artifact_mode,
            sandbox_root=sandbox_root,
        )
        self.retain_artifacts = self.artifact_policy.write_traces
        self.short_term = ShortTermGraph()
        self.long_term = LongTermGraph()
        self.message_board = MessageBoard()
        self.open_handles = OpenHandleTable()
        self.predictors = predictors or DecisionFamilyModelBank()
        self._shared_predictors = predictors is not None
        self.agent_pool = AgentPool()
        self.safety_guard = SafetyGuard()
        self.sandbox_manager = SandboxManager(self.artifact_policy.sandbox_root)
        self.tool_registry = ToolRegistry(self.sandbox_manager, self.safety_guard)
        self.tool_executor = ToolExecutor(
            self.tool_registry,
            self.sandbox_manager,
            persist_artifacts=self.artifact_policy.persist_tool_artifacts,
        )
        self.trace_dir = self.workspace / "traces"
        self.checkpoint_dir = self.workspace / "checkpoints"
        self._resume_checkpoint_store_dir: Path | None = None
=======
class FixedShell:
    def __init__(
        self,
        workspace: Path,
        predictors: DecisionFamilyModelBank | None = None,
        *,
        artifact_mode: str | ArtifactMode | None = None,
        sandbox_root: Path | None = None,
        run_store: RunStore | None = None,
        run_id: str = "",
        attempt_id: str = "",
    ) -> None:
        self.workspace = Path(workspace)
        self.run_store = run_store
        self.run_id = run_id
        self.attempt_id = attempt_id
        self.run_root = Path(run_store.run_root).resolve() if run_store is not None and run_store.run_root is not None else self.workspace
        self.artifact_policy = ArtifactPolicy.resolve(
            artifact_mode=artifact_mode,
            sandbox_root=sandbox_root,
        )
        self.retain_artifacts = self.artifact_policy.write_traces
        self.short_term = ShortTermGraph()
        self.long_term = LongTermGraph()
        self.message_board = MessageBoard()
        self.open_handles = OpenHandleTable()
        self.predictors = predictors or DecisionFamilyModelBank()
        self._shared_predictors = predictors is not None
        self.agent_pool = AgentPool()
        self.safety_guard = SafetyGuard()
        self.sandbox_manager = SandboxManager(self.artifact_policy.sandbox_root)
        self.tool_registry = ToolRegistry(self.sandbox_manager, self.safety_guard)
        self.tool_executor = ToolExecutor(
            self.tool_registry,
            self.sandbox_manager,
            persist_artifacts=self.artifact_policy.persist_tool_artifacts,
        )
        self.trace_dir = (self.run_root / "traces") if run_store is not None else (self.workspace / "traces")
        self.checkpoint_dir = (self.run_root / "checkpoints") if run_store is not None else (self.workspace / "checkpoints")
        self._resume_checkpoint_store_dir: Path | None = None
>>>>>>> REPLACE

File: `agintor/shell.py`
<<<<<<< SEARCH
    def save_trace(self, task_id: str, seed: int, trace: list[dict[str, Any]]) -> Path | None:
        if not self.retain_artifacts:
            return None
        ensure_directory(self.trace_dir)
        path = self.trace_dir / f"{task_id.replace('/', '_')}_{seed}.json"
        path.write_text(json.dumps(trace, indent=2, sort_keys=True), encoding="utf-8")
        return path
=======
    def save_trace(self, task_id: str, seed: int, trace: list[dict[str, Any]]) -> Path | None:
        if not self.retain_artifacts:
            return None
        ensure_directory(self.trace_dir)
        prefix = f"{self.attempt_id}." if self.attempt_id else ""
        path = self.trace_dir / f"{prefix}{task_id.replace('/', '_')}_{seed}.json"
        path.write_text(json.dumps(trace, indent=2, sort_keys=True), encoding="utf-8")
        return path
>>>>>>> REPLACE

File: `agintor/shell.py`
<<<<<<< SEARCH
    def save_checkpoint_envelope(self, envelope: CheckpointEnvelope) -> CheckpointReference:
        request_dir = ensure_directory(self.checkpoint_dir / envelope.request_id)
        path = request_dir / f"{envelope.checkpoint_id}.json"
        path.write_text(json.dumps(model_dump(envelope), indent=2, sort_keys=True), encoding="utf-8")
        index_path = request_dir / "index.json"
        index_rows: list[dict[str, Any]] = []
        if index_path.exists():
            try:
                payload = json.loads(index_path.read_text(encoding="utf-8"))
            except Exception:
                payload = []
            if isinstance(payload, list):
                index_rows = [dict(row) for row in payload if isinstance(row, dict)]
        index_rows.append(
            {
                "checkpoint_ref": str(path),
                "checkpoint_id": envelope.checkpoint_id,
                "sequence_no": envelope.sequence_no,
                "boundary": envelope.boundary,
                "created_at": envelope.created_at,
            }
        )
        index_rows.sort(key=lambda row: (int(row.get("sequence_no", 0) or 0), str(row.get("checkpoint_id", ""))))
        index_path.write_text(json.dumps(index_rows, indent=2, sort_keys=True), encoding="utf-8")
        latest_path = request_dir / "LATEST.json"
        latest_path.write_text(
            json.dumps(
                {
                    "checkpoint_ref": str(path),
                    "checkpoint_id": envelope.checkpoint_id,
                    "sequence_no": envelope.sequence_no,
                    "boundary": envelope.boundary,
                    "created_at": envelope.created_at,
                },
                indent=2,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        checkpoint_count = len(index_rows)
        return CheckpointReference(
            ref=str(path),
            task_id=envelope.task_id,
            seed=envelope.seed,
            request_id=envelope.request_id,
            plan_id=envelope.plan_id,
            checkpoint_id=envelope.checkpoint_id,
            sequence_no=envelope.sequence_no,
            boundary=envelope.boundary,
            created_at=envelope.created_at,
            checkpoint_count=checkpoint_count,
            latest=True,
        )
=======
    def save_checkpoint_envelope(self, envelope: CheckpointEnvelope) -> CheckpointReference:
        if self.run_store is not None:
            return self.run_store.write_checkpoint(envelope)
        request_dir = ensure_directory(self.checkpoint_dir / envelope.request_id)
        path = request_dir / f"{envelope.checkpoint_id}.json"
        path.write_text(json.dumps(model_dump(envelope), indent=2, sort_keys=True), encoding="utf-8")
        return CheckpointReference(
            ref=str(path),
            run_id=envelope.run_id,
            run_root=envelope.run_root,
            attempt_id=envelope.attempt_id,
            task_id=envelope.task_id,
            seed=envelope.seed,
            request_id=envelope.request_id,
            plan_id=envelope.plan_id,
            checkpoint_id=envelope.checkpoint_id,
            sequence_no=envelope.sequence_no,
            boundary=envelope.boundary,
            created_at=envelope.created_at,
            checkpoint_count=1,
            latest=True,
        )
>>>>>>> REPLACE

File: `agintor/shell.py`
<<<<<<< SEARCH
    def latest_checkpoint_ref(self, request_id: str, checkpoint_store_dir: str | Path | None = None) -> str | None:
        for checkpoint_root in self._checkpoint_lookup_roots(checkpoint_store_dir):
            request_dir = checkpoint_root / request_id
            index_path = request_dir / "index.json"
            if index_path.exists():
                try:
                    payload = json.loads(index_path.read_text(encoding="utf-8"))
                except Exception:
                    payload = []
                if isinstance(payload, list) and payload:
                    latest = max(
                        (dict(row) for row in payload if isinstance(row, dict)),
                        key=lambda row: (int(row.get("sequence_no", 0) or 0), str(row.get("checkpoint_id", ""))),
                    )
                    checkpoint_ref = self._checkpoint_path_from_index_row(request_dir, latest)
                    if checkpoint_ref:
                        return checkpoint_ref
            latest_path = request_dir / "LATEST.json"
            if not latest_path.exists():
                continue
            try:
                payload = json.loads(latest_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            checkpoint_ref = self._checkpoint_path_from_index_row(request_dir, payload)
            if checkpoint_ref:
                return checkpoint_ref
        return None
=======
    def latest_checkpoint_ref(self, run_ref: str | None = None, checkpoint_store_dir: str | Path | None = None) -> str | None:
        if self.run_store is not None:
            return self.run_store.latest_checkpoint_ref(run_ref or self.run_id or self.run_root)
        for checkpoint_root in self._checkpoint_lookup_roots(checkpoint_store_dir):
            request_dir = checkpoint_root / str(run_ref)
            latest_path = request_dir / "LATEST.json"
            if latest_path.exists():
                payload = json.loads(latest_path.read_text(encoding="utf-8"))
                checkpoint_ref = self._checkpoint_path_from_index_row(request_dir, payload)
                if checkpoint_ref:
                    return checkpoint_ref
        return None
>>>>>>> REPLACE

File: `agintor/shell.py`
<<<<<<< SEARCH
    def load_checkpoint_envelope(
        self,
        *,
        checkpoint_ref: str | None = None,
        request_id: str | None = None,
        checkpoint_store_dir: str | Path | None = None,
    ) -> CheckpointEnvelope:
        target_ref = str(checkpoint_ref or "").strip()
        if not target_ref:
            if not request_id:
                raise FileNotFoundError("resume requires checkpoint_ref or request_id")
            latest = self.latest_checkpoint_ref(str(request_id), checkpoint_store_dir=checkpoint_store_dir)
            if not latest:
                raise FileNotFoundError(f"no latest checkpoint published for request {request_id}")
            target_ref = latest
        path = Path(target_ref)
        payload = json.loads(path.read_text(encoding="utf-8"))
        return model_validate(CheckpointEnvelope, payload)
=======
    def load_checkpoint_envelope(
        self,
        *,
        checkpoint_ref: str | None = None,
        run_ref: str | None = None,
        request_id: str | None = None,
        checkpoint_store_dir: str | Path | None = None,
    ) -> CheckpointEnvelope:
        target_ref = str(checkpoint_ref or "").strip()
        if not target_ref:
            target_ref = str(self.latest_checkpoint_ref(run_ref or self.run_id or request_id, checkpoint_store_dir=checkpoint_store_dir) or "").strip()
        if not target_ref:
            raise FileNotFoundError("resume requires run_ref or checkpoint_ref")
        path = Path(target_ref)
        payload = json.loads(path.read_text(encoding="utf-8"))
        return model_validate(CheckpointEnvelope, payload)
>>>>>>> REPLACE

## `agintor/runner.py`

Comment
- Keep the runtime-owned run identity on every checkpoint and result.
- Do not derive checkpoint IDs from `request_id` anymore.

File: `agintor/runner.py`
<<<<<<< SEARCH
        context.state.latest_checkpoint_ref = self.shell.latest_checkpoint_ref(checkpoint_envelope.request_id)
=======
        context.state.latest_checkpoint_ref = self.shell.latest_checkpoint_ref(
            checkpoint_envelope.run_id or checkpoint_envelope.run_root or checkpoint_envelope.request_id
        )
>>>>>>> REPLACE

File: `agintor/runner.py`
<<<<<<< SEARCH
        envelope = CheckpointEnvelope(
            checkpoint_id=f"checkpoint.{plan.request_id}.{context.state.checkpoint_sequence_no:04d}",
            runtime_abi=self.runtime.kernel_manifest.runtime_abi,
            storage_schema_version=self.runtime.kernel_manifest.storage_schema_version,
            runtime_hash=self.runtime.runtime_hash,
            request_id=plan.request_id,
            plan_id=plan.plan_id,
            task_id=task.task_id,
            seed=seed,
=======
        envelope = CheckpointEnvelope(
            checkpoint_id=f"checkpoint.{(context.shell.run_id or plan.request_id)}.{context.state.checkpoint_sequence_no:04d}",
            runtime_abi=self.runtime.kernel_manifest.runtime_abi,
            storage_schema_version=self.runtime.kernel_manifest.storage_schema_version,
            runtime_hash=self.runtime.runtime_hash,
            run_id=getattr(context.shell, "run_id", ""),
            run_root=str(getattr(context.shell, "run_root", context.shell.workspace)),
            attempt_id=getattr(context.shell, "attempt_id", ""),
            request_id=plan.request_id,
            plan_id=plan.plan_id,
            task_id=task.task_id,
            seed=seed,
>>>>>>> REPLACE

File: `agintor/runner.py`
<<<<<<< SEARCH
        return RunResult(
            request_id=plan.request_id,
            plan_id=plan.plan_id,
            task_id=task.task_id,
            seed=seed,
            artifact=artifact,
            verifier_score=verifier_score,
            cost=budget.cost,
            latency=time.perf_counter() - start,
            faults=faults,
            trace=[dict(row) for row in trace],
            trace_context=plan.trace_context,
            trace_path=str(trace_path) if trace_path is not None else None,
            checkpoint_ref=state.latest_checkpoint_ref or self.shell.latest_checkpoint_ref(plan.request_id),
            hard_invalid=hard_invalid,
            invalid_reason=invalid_reason,
            failure_kind=failure_kind if hard_invalid else None,
            mode=state.mode,
            lifecycle_state=state.execution_state,
=======
        latest_checkpoint_ref = state.latest_checkpoint_ref or self.shell.latest_checkpoint_ref(
            getattr(self.shell, "run_id", "") or plan.request_id
        )
        return RunResult(
            request_id=plan.request_id,
            plan_id=plan.plan_id,
            run_id=getattr(self.shell, "run_id", ""),
            run_root=str(getattr(self.shell, "run_root", self.shell.workspace)),
            attempt_id=getattr(self.shell, "attempt_id", ""),
            latest_checkpoint_ref=latest_checkpoint_ref,
            run_lifecycle_state="failed" if hard_invalid and not latest_checkpoint_ref else ("paused" if hard_invalid else "completed"),
            run_resumable=bool(latest_checkpoint_ref),
            run_prune_eligible=bool(hard_invalid and not latest_checkpoint_ref),
            task_id=task.task_id,
            seed=seed,
            artifact=artifact,
            verifier_score=verifier_score,
            cost=budget.cost,
            latency=time.perf_counter() - start,
            faults=faults,
            trace=[dict(row) for row in trace],
            trace_context=plan.trace_context,
            trace_path=str(trace_path) if trace_path is not None else None,
            checkpoint_ref=latest_checkpoint_ref,
            hard_invalid=hard_invalid,
            invalid_reason=invalid_reason,
            failure_kind=failure_kind if hard_invalid else None,
            mode=state.mode,
            lifecycle_state=state.execution_state,
>>>>>>> REPLACE

## `agintor/runtime_api.py`

Comment
- Thread run identity and lifecycle metadata into `SolveResult`.
- Ensure shaped failures still carry run identity.

File: `agintor/runtime_api.py`
<<<<<<< SEARCH
def runtime_solve_failure_response(
    request: SolveRequest,
    runtime_hash: str,
    capability_exchange: CapabilityExchange,
    *,
    mode: str,
    summary: str,
    provider_usage: dict[str, Any] | None = None,
    fault_code: str = "solve_failure",
) -> RuntimeSolveResponse:
=======
def runtime_solve_failure_response(
    request: SolveRequest,
    runtime_hash: str,
    capability_exchange: CapabilityExchange,
    *,
    mode: str,
    summary: str,
    provider_usage: dict[str, Any] | None = None,
    fault_code: str = "solve_failure",
    run_id: str = "",
    run_root: str = "",
    attempt_id: str = "",
    latest_checkpoint_ref: str | None = None,
) -> RuntimeSolveResponse:
>>>>>>> REPLACE

File: `agintor/runtime_api.py`
<<<<<<< SEARCH
        solve_result=SolveResult(
            request_id=request.request_id,
            runtime_hash=runtime_hash,
            mode=mode,
            artifact={"error": fault_code, "message": summary},
            status="failed",
            verification_status="failed",
            summary=summary,
            checks=[],
            budget={},
            provider_usage=dict(provider_usage or {}),
            faults={
                "count": 1,
                "hard_invalid": False,
                "invalid_reason": summary,
                "code": fault_code,
                "contract_error": True,
            },
            recoverability="none",
            verified=False,
            best_effort=False,
        ),
=======
        solve_result=SolveResult(
            request_id=request.request_id,
            runtime_hash=runtime_hash,
            run_id=run_id,
            run_root=run_root,
            attempt_id=attempt_id,
            latest_checkpoint_ref=latest_checkpoint_ref,
            run_lifecycle_state="failed",
            run_resumable=bool(latest_checkpoint_ref),
            run_prune_eligible=not bool(latest_checkpoint_ref),
            mode=mode,
            artifact={"error": fault_code, "message": summary},
            status="failed",
            verification_status="failed",
            summary=summary,
            checks=[],
            budget={},
            provider_usage=dict(provider_usage or {}),
            faults={
                "count": 1,
                "hard_invalid": False,
                "invalid_reason": summary,
                "code": fault_code,
                "contract_error": True,
            },
            recoverability="checkpoint_available" if latest_checkpoint_ref else "none",
            verified=False,
            best_effort=False,
        ),
>>>>>>> REPLACE

File: `agintor/runtime_api.py`
<<<<<<< SEARCH
    recoverability = "none"
    if run.checkpoint_ref:
        recoverability = "checkpoint_available"
    elif not run.hard_invalid and not controlled_failure:
        recoverability = "terminal"
    return SolveResult(
        request_id=request.request_id,
        runtime_hash=runtime_hash,
        mode=mode,
        artifact=run.artifact,
        status=status,
        verification_status=verification_status,
        summary=summary,
        checks=checks,
        trace_ref=run.trace_ref(),
        checkpoint_ref=run.checkpoint_ref,
        budget={
=======
    latest_checkpoint_ref = run.latest_checkpoint_ref or run.checkpoint_ref
    recoverability = "none"
    if latest_checkpoint_ref:
        recoverability = "checkpoint_available"
    elif not run.hard_invalid and not controlled_failure:
        recoverability = "terminal"
    return SolveResult(
        request_id=request.request_id,
        runtime_hash=runtime_hash,
        run_id=run.run_id,
        run_root=run.run_root,
        attempt_id=run.attempt_id,
        latest_checkpoint_ref=latest_checkpoint_ref,
        run_lifecycle_state=run.run_lifecycle_state,
        run_resumable=run.run_resumable,
        run_prune_eligible=run.run_prune_eligible,
        mode=mode,
        artifact=run.artifact,
        status=status,
        verification_status=verification_status,
        summary=summary,
        checks=checks,
        trace_ref=run.trace_ref(),
        checkpoint_ref=run.checkpoint_ref,
        budget={
>>>>>>> REPLACE

## `tests/test_runtime_host.py`

Comment
- Replace the request-id global-scan expectations with durable `run_ref` / `checkpoint_ref` expectations.
- Cover solve-created run roots, resume preflight reuse, and prune rules.

File: `tests/test_runtime_host.py`
<<<<<<< SEARCH
from agintor.schemas import (
    BenchmarkTask,
    CapabilityExchange,
    OpenAITraceContext,
    ResumeRequest,
    RuntimeIsolationPolicy,
    RuntimeResumeRequest,
    RuntimeSolveResponse,
    SolveResult,
)
=======
from agintor.run_store import RunStore
from agintor.schemas import (
    AttemptManifest,
    BenchmarkTask,
    CapabilityExchange,
    OpenAITraceContext,
    ResumeRequest,
    RunManifest,
    RuntimeIsolationPolicy,
    RuntimeResumeRequest,
    RuntimeSolveRequest,
    RuntimeSolveResponse,
    SolveResult,
)
>>>>>>> REPLACE

File: `tests/test_runtime_host.py`
<<<<<<< SEARCH
def test_runtime_host_resume_resolves_latest_checkpoint_from_retained_store(monkeypatch, tmp_path: Path):
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    runtime_profile = _runtime_profile()
    provider = build_provider(runtime_profile.runtime_provider.name, provider_profile=runtime_profile.runtime_provider)
    request_dir = host.workspace / ".runtime_host" / "prior_run" / "workspace" / "seed_0" / "checkpoints" / "resume.test"
    request_dir.mkdir(parents=True)
    checkpoint_path = request_dir / "checkpoint.resume.test.0002.json"
    checkpoint_path.write_text("{}", encoding="utf-8")
    latest_path = request_dir / "LATEST.json"
    latest_path.write_text(
        json.dumps(
            {
                "checkpoint_ref": "/mnt/workspace/seed_0/checkpoints/resume.test/checkpoint.resume.test.0002.json",
                "checkpoint_id": "checkpoint.resume.test.0002",
                "sequence_no": 2,
                "boundary": "after_branch_completion",
                "created_at": 2.0,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    request = ResumeRequest(request_id="resume.test")
    capability_exchange = _capability_exchange()
    monkeypatch.setattr(host, "inspect", lambda runtime_dir: capability_exchange)
    captured: dict[str, RuntimeResumeRequest] = {}
    response = RuntimeSolveResponse(
        request_id=request.request_id,
        capability_exchange=capability_exchange,
        solve_result=SolveResult(
            request_id=request.request_id,
            runtime_hash="hash",
            mode="user_request",
            artifact={"status": "resumed"},
            status="best_effort",
            verification_status="best_effort",
            summary="ok",
            checks=[],
            budget={},
            provider_usage={},
            faults={"hard_invalid": False},
            recoverability="terminal",
            verified=False,
            best_effort=True,
        ),
    )

    def succeed(runtime_dir, runtime_request, **kwargs):
        captured["request"] = runtime_request
        return response

    monkeypatch.setattr(host, "_run_local_resume", succeed)
    host.resume("dummy-runtime", request, provider=provider, runtime_profile=runtime_profile)
    assert isinstance(captured["request"], RuntimeResumeRequest)
    assert captured["request"].checkpoint_ref == str(checkpoint_path.resolve())
    assert captured["request"].checkpoint_store_dir == str(request_dir.parent.resolve())
=======
def test_runtime_host_solve_creates_durable_run_root_and_returns_identity(monkeypatch, tmp_path: Path):
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    runtime_profile = _runtime_profile()
    provider = build_provider(runtime_profile.runtime_provider.name, provider_profile=runtime_profile.runtime_provider)
    request = runtime_solve_request_for_user_request(
        runtime_backend="local",
        seed=0,
        solve_request=load_solve_request(prompt="Say hello."),
    )
    capability_exchange = _capability_exchange()
    monkeypatch.setattr(host, "inspect", lambda runtime_dir: capability_exchange)
    response = RuntimeSolveResponse(
        request_id=request.request_id,
        capability_exchange=capability_exchange,
        solve_result=SolveResult(
            request_id=request.request_id,
            runtime_hash="hash",
            run_id="run.123",
            run_root=str(tmp_path / "host" / "runs" / "run.123"),
            attempt_id="attempt_0001",
            latest_checkpoint_ref=str(tmp_path / "host" / "runs" / "run.123" / "checkpoints" / "checkpoint.0001.json"),
            run_lifecycle_state="completed",
            run_resumable=True,
            mode="user_request",
            artifact="hello",
            status="best_effort",
            verification_status="best_effort",
            summary="ok",
            checks=[],
            budget={},
            provider_usage={},
            faults={"hard_invalid": False},
            recoverability="checkpoint_available",
            verified=False,
            best_effort=True,
        ),
    )
    monkeypatch.setattr(host, "_run_local_solve", lambda *args, **kwargs: response)

    solved = host.solve("dummy-runtime", request, provider=provider, runtime_profile=runtime_profile)
    assert solved.solve_result.run_id
    assert solved.solve_result.run_root.endswith(solved.solve_result.run_id)
    assert solved.solve_result.attempt_id == "attempt_0001"
    assert solved.solve_result.latest_checkpoint_ref


def test_runtime_host_resume_uses_run_ref_and_replays_solve_preflight(monkeypatch, tmp_path: Path):
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    runtime_profile = _runtime_profile()
    provider = build_provider(runtime_profile.runtime_provider.name, provider_profile=runtime_profile.runtime_provider)
    capability_exchange = _capability_exchange()
    original_request = runtime_solve_request_for_user_request(
        runtime_backend="local",
        seed=0,
        solve_request=load_solve_request(prompt="Say hello."),
    )
    run_manifest = RunManifest(
        run_id="run.123",
        run_root=str(tmp_path / "host" / "runs" / "run.123"),
        request_id=original_request.request_id,
        request_mode="user_request",
        runtime_backend="local",
    )
    attempt_manifest = AttemptManifest(
        attempt_id="attempt_0002",
        run_id=run_manifest.run_id,
        run_root=run_manifest.run_root,
        sequence_no=2,
        launch_kind="resume",
        workspace_root=str(Path(run_manifest.run_root) / "attempts" / "attempt_0002" / "workspace"),
    )
    checkpoint_path = tmp_path / "resume-checkpoint.json"
    checkpoint_path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(host, "inspect", lambda runtime_dir: capability_exchange)
    monkeypatch.setattr(
        host.run_store,
        "resolve_resume_target",
        lambda **kwargs: type("ResumeTarget", (), {"run_manifest": run_manifest, "checkpoint_path": checkpoint_path})(),
    )
    monkeypatch.setattr(
        host.run_store,
        "load_request_bundle",
        lambda manifest: {"request_kind": "runtime_solve_request", "payload": model_dump(original_request)},
    )
    monkeypatch.setattr(host.run_store, "begin_attempt", lambda *args, **kwargs: attempt_manifest)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        host,
        "_preflight_solve_contract",
        lambda runtime_dir, capability_exchange, request, **kwargs: captured.setdefault("preflight_request", request),
    )
    response = RuntimeSolveResponse(
        request_id=original_request.request_id,
        capability_exchange=capability_exchange,
        solve_result=SolveResult(
            request_id=original_request.request_id,
            runtime_hash="hash",
            run_id=run_manifest.run_id,
            run_root=run_manifest.run_root,
            attempt_id=attempt_manifest.attempt_id,
            latest_checkpoint_ref=str(checkpoint_path),
            run_lifecycle_state="paused",
            run_resumable=True,
            mode="user_request",
            artifact={"status": "resumed"},
            status="best_effort",
            verification_status="best_effort",
            summary="ok",
            checks=[],
            budget={},
            provider_usage={},
            faults={"hard_invalid": False},
            recoverability="checkpoint_available",
            verified=False,
            best_effort=True,
        ),
    )
    monkeypatch.setattr(host, "_run_local_resume", lambda *args, **kwargs: response)

    resumed = host.resume(
        "dummy-runtime",
        ResumeRequest(run_ref=run_manifest.run_id),
        provider=provider,
        runtime_profile=runtime_profile,
    )

    assert isinstance(captured["preflight_request"], RuntimeSolveRequest)
    assert captured["preflight_request"].request_id == original_request.request_id
    assert resumed.solve_result.run_id == run_manifest.run_id
    assert resumed.solve_result.attempt_id == attempt_manifest.attempt_id


def test_runtime_host_only_prunes_failed_non_resumable_runs(monkeypatch, tmp_path: Path):
    host = RuntimeHost(tmp_path / "host", runtime_backend="local")
    runtime_profile = _runtime_profile()
    provider = build_provider(runtime_profile.runtime_provider.name, provider_profile=runtime_profile.runtime_provider)
    request = runtime_solve_request_for_user_request(
        runtime_backend="local",
        seed=0,
        solve_request=load_solve_request(prompt="Say hello."),
    )
    capability_exchange = _capability_exchange()
    monkeypatch.setattr(host, "inspect", lambda runtime_dir: capability_exchange)
    pruned: dict[str, bool] = {"called": False}
    monkeypatch.setattr(host.run_store, "prune_run", lambda manifest: pruned.__setitem__("called", True) or manifest.copy(update={"lifecycle_state": "pruned"}))
    response = RuntimeSolveResponse(
        request_id=request.request_id,
        capability_exchange=capability_exchange,
        solve_result=SolveResult(
            request_id=request.request_id,
            runtime_hash="hash",
            run_id="run.123",
            run_root=str(tmp_path / "host" / "runs" / "run.123"),
            attempt_id="attempt_0001",
            latest_checkpoint_ref=None,
            run_lifecycle_state="failed",
            run_resumable=False,
            run_prune_eligible=True,
            mode="user_request",
            artifact={"error": "boom"},
            status="failed",
            verification_status="failed",
            summary="boom",
            checks=[],
            budget={},
            provider_usage={},
            faults={"hard_invalid": True, "code": "hard_invalid"},
            recoverability="none",
            verified=False,
            best_effort=False,
        ),
    )
    monkeypatch.setattr(host, "_run_local_solve", lambda *args, **kwargs: response)

    host.solve("dummy-runtime", request, provider=provider, runtime_profile=runtime_profile)
    assert pruned["called"] is True
>>>>>>> REPLACE

## `tests/test_run_store.py`

Comment
- New focused tests for run-root creation, attempt numbering, checkpoint indexing, and prune rules.

File: `tests/test_run_store.py`
<<<<<<< SEARCH
=======
from __future__ import annotations

from pathlib import Path

from agintor.run_store import RunStore
from agintor.schemas import CheckpointEnvelope


def test_run_store_creates_durable_run_root_and_monotonic_attempt_ids(tmp_path: Path):
    store = RunStore(tmp_path)
    manifest = store.create_run(
        request_id="req-1",
        request_mode="user_request",
        runtime_backend="local",
        trace_context=None,
        task_id="task-1",
        seed=7,
    )
    attempt_one = store.begin_attempt(manifest, launch_kind="solve")
    attempt_two = store.begin_attempt(store.load_run_manifest(manifest.run_root), launch_kind="resume")

    assert Path(manifest.run_root).exists()
    assert attempt_one.attempt_id == "attempt_0001"
    assert attempt_two.attempt_id == "attempt_0002"
    assert Path(attempt_two.workspace_root).exists()


def test_run_store_indexes_latest_checkpoint_per_run_root(tmp_path: Path):
    store = RunStore(tmp_path)
    manifest = store.create_run(
        request_id="req-1",
        request_mode="benchmark",
        runtime_backend="local",
        trace_context=None,
        task_id="task-1",
        seed=1,
    )
    attempt = store.begin_attempt(manifest, launch_kind="solve")
    ref_one = store.write_checkpoint(
        CheckpointEnvelope(
            checkpoint_id="checkpoint.run.0001",
            runtime_abi="agintor-runtime-abi-v4",
            storage_schema_version="agintor-storage-v2",
            runtime_hash="runtime-hash",
            run_id=manifest.run_id,
            run_root=manifest.run_root,
            attempt_id=attempt.attempt_id,
            request_id=manifest.request_id,
            plan_id="plan-1",
            task_id="task-1",
            seed=1,
            sequence_no=1,
            boundary="after_provider_completion",
        )
    )
    ref_two = store.write_checkpoint(
        CheckpointEnvelope(
            checkpoint_id="checkpoint.run.0002",
            runtime_abi="agintor-runtime-abi-v4",
            storage_schema_version="agintor-storage-v2",
            runtime_hash="runtime-hash",
            run_id=manifest.run_id,
            run_root=manifest.run_root,
            attempt_id=attempt.attempt_id,
            request_id=manifest.request_id,
            plan_id="plan-1",
            task_id="task-1",
            seed=1,
            sequence_no=2,
            boundary="after_merge",
        )
    )

    assert ref_one.sequence_no == 1
    assert ref_two.sequence_no == 2
    assert store.latest_checkpoint_ref(manifest.run_id) == ref_two.ref
    assert store.load_run_manifest(manifest.run_id).latest_checkpoint_ref == ref_two.ref


def test_run_store_prune_retains_manifest_but_drops_resume_payloads(tmp_path: Path):
    store = RunStore(tmp_path)
    manifest = store.create_run(
        request_id="req-1",
        request_mode="user_request",
        runtime_backend="local",
        trace_context=None,
    )
    manifest = store.finish_run(
        manifest,
        lifecycle_state="failed",
        latest_checkpoint_ref=None,
        resumable=False,
        failure_kind="hard_invalid",
    )
    pruned = store.prune_run(manifest)

    assert pruned.lifecycle_state == "pruned"
    assert (Path(pruned.run_root) / "run_manifest.json").exists()
    assert not (Path(pruned.run_root) / "checkpoints").exists()
    assert not (Path(pruned.run_root) / "traces").exists()
>>>>>>> REPLACE

## Follow-on notes for the orchestrator

- Worker 02 should add the `runtime_state_snapshot`, `shell_state_snapshot`, and `attempt_snapshot` fields to `CheckpointEnvelope` after the run-identity additions above land, and should route receipt persistence through `RunStore.side_effects_dir` so resume does not depend on one later checkpoint surviving.
- Worker 03 should consume the stamped `run_id` / `run_root` / `attempt_id` on grouped batch invocations, add the episode-order sort, and keep grouped invocations in one durable lineage until or unless the runtime contract explicitly changes.
- The docker executor should mirror the new run-root transport by mounting the durable attempt workspace, rewriting `run_root` and `latest_checkpoint_ref`, and resolving `run_ref` relative to the mounted run root instead of the old `checkpoint_store_dir` plus request-id directory heuristic.
