# WS4 Engineering Design

## Outcome

Workstream 4 turns benchmark planning, evaluation, and search from demo-shaped mechanics into a reproducible factory evidence system.

The implementation must produce:

- a plan-scoped benchmark registry that is the only authority for evaluation task selection
- typed benchmark adapters and frozen fixtures for serious `repo_patch` and `service_task` lanes
- typed verifier catalogs with replayable disk-only evidence
- durable evaluation ledgers, held-out reports, contamination records, and signal-sufficiency reports
- resumable search state with fail-closed manifest validation
- deterministic objective and operator selection with auditable choice records
- factory-side trace stamping for planning, mutation, patch repair, objective choice, and operator choice

This document is the engineering supplement for `implementation_workstreams/WORKSTREAM_4_BENCHMARKS_EVALUATION_AND_SEARCH.md`. It is intentionally one level above code.

## Boundaries

WS4 owns benchmark content, fixture contracts, verifier evidence, evaluation reporting, search policy, search-state persistence, held-out reporting, contamination controls, and factory-side trace stamping for search calls.

WS4 must not:

- import `agintor/task_runtime/*` from factory evaluation code
- create a second run/checkpoint/trace durability authority
- redesign WS3 trace topology or state-store schemas
- generate free-form provider-authored grader code
- make internet-dependent benchmark lanes part of the MVP proof
- leak validation or test evidence into mutation prompts

All benchmark execution must continue through `RuntimeHost` and the bundled runtime entrypoint.

## Artifact Layout

Build workspace artifacts:

```text
planning/
  goal_spec.json
  success_criteria.json
  benchmark_suite.json
  benchmark_plan.json
  benchmark_provenance.json
  verifier_bundle.json
  fixture_catalog.json

evolution/
  search_state.json
  search_resume_manifest.json
  signal_sufficiency.json
  held_out_report.json
  proof_campaign.json
  objective_choices.json
  operator_choices.json
  validation_history.json
  stage_failures.json
  leaderboard.json
  evolution_history.json
  archive_index.json
  verifier_evidence_index.json
  evaluations/
    <runtime_hash>.suite_evaluation.json
  verifier_evidence/
    <evidence_id>.json
  predictors/
    predictor_snapshot.<iteration>.json
```

Repository-owned benchmark fixtures:

```text
agintor/benchmark_fixtures/
  catalog.json
  structured_ops/*.json
  repo_patch/<fixture_id>/fixture.json
  repo_patch/<fixture_id>/repo_snapshot/
  service_task/<fixture_id>/fixture.json
  browser_task/<fixture_id>/fixture.json
  multimodal_task/<fixture_id>/fixture.json
```

## 1. Plan-Scoped Benchmark Registry

### Schemas

Add these literals in `agintor/schemas.py`:

```python
BenchmarkPartition = Literal["train", "proxy", "val", "test"]
BenchmarkFamily = Literal["top", "mem", "tool", "e2e"]
BenchmarkAdapterKind = Literal[
    "structured_ops",
    "repo_patch",
    "service_task",
    "browser_task",
    "multimodal_task",
]
BenchmarkSelectionKind = Literal[
    "library",
    "goal_clone",
    "template_variant",
    "synthetic",
]
```

Add `BenchmarkFixtureRef`:

- `fixture_id: str`
- `adapter_kind: BenchmarkAdapterKind`
- `artifact_id: str`
- `content_ref: str`
- `content_digest: str`
- `environment_digest: str`
- `source_path: str = ""`
- `writable_roots: list[str] = []`
- `readonly_roots: list[str] = []`
- `setup_digest: str = ""`
- `teardown_policy: str = "discard"`

Add `BenchmarkPartitionEntry`:

- `entry_id: str`
- `plan_id: str`
- `task_id: str`
- `partition: BenchmarkPartition`
- `family: BenchmarkFamily`
- `adapter_kind: BenchmarkAdapterKind`
- `fixture_ids: list[str]`
- `fixture_refs: list[BenchmarkFixtureRef]`
- `environment_digest: str`
- `verifier_ids: list[str]`
- `contamination_flags: list[str]`
- `source_task_id: str | None`
- `template_id: str | None`
- `goal_criteria_targets: list[str]`
- `transform_summary: str`
- `selection_kind: BenchmarkSelectionKind`
- `task_payload_digest: str`
- `source_suite_id: str`
- `source_suite_partition: BenchmarkPartition`
- `verifier_origin: str`

`entry_id` is a stable hash over `plan_id`, `partition`, `task_id`, `fixture_ids`, `verifier_ids`, and `task_payload_digest`.

Extend `BenchmarkPlan`:

- `planning_strategy: str = "goal_scoped_multi_select_v1"`
- `partition_entries: list[BenchmarkPartitionEntry]`
- keep `train_task_ids`, `proxy_task_ids`, `val_task_ids`, `test_task_ids`, and `synthetic_task_ids` as compatibility projections only

Validation rule: when `partition_entries` is present, the old task-id lists must exactly equal grouped entry task IDs. Legacy list-only plans are accepted only by an explicit migration helper.

Add `BenchmarkProvenance`:

- `provenance_id: str`
- `schema_version: str = "agintor.benchmark-provenance.v1"`
- `build_id: str`
- `goal_id: str`
- `benchmark_plan_id: str`
- `verifier_bundle_id: str`
- `suite_id: str`
- `planning_strategy: str`
- `raw_goal_reparse_allowed: bool = False`
- `artifact_digests: dict[str, str]`
- `family_scores: dict[str, float]`
- `selected_families: list[str]`
- `selection_rules: dict[str, Any]`
- `partition_entries: list[BenchmarkPartitionEntry]`
- `required_capabilities: list[str]`
- `capability_coverage: list[BenchmarkCoverageRecord]`
- `success_criteria_coverage: list[BenchmarkCoverageRecord]`
- `source_task_provenance: list[BenchmarkSelectionRecord]`
- `template_provenance: list[BenchmarkSelectionRecord]`
- `synthesis_decisions: list[BenchmarkSelectionRecord]`
- `provider_assist: ProviderAssistRecord`
- `contamination_records: list[ContaminationRecord]`
- `created_at: str`

### Registry API

Add in `agintor/benchmarks.py`:

- `PlanScopedTaskRegistry`
- `BenchmarkAdapterSpec`
- `BenchmarkAdapterRegistry`
- `register_benchmark_adapter(adapter: BenchmarkAdapter) -> None`
- `default_benchmark_adapter_registry() -> BenchmarkAdapterRegistry`
- `adapter_kind_for_task(task: BenchmarkTask) -> BenchmarkAdapterKind`
- `load_frozen_benchmark_suite(path: Path) -> BenchmarkSuite`
- `load_fixture_catalog(path: Path) -> BenchmarkFixtureCatalog`
- `build_plan_scoped_task_registry(plan, suite, verifier_bundle, provenance, fixture_catalog) -> PlanScopedTaskRegistry`
- `migrate_legacy_benchmark_plan(plan, suite, verifier_bundle) -> BenchmarkPlan`

`PlanScopedTaskRegistry` exposes:

- `tasks(partition: BenchmarkPartition) -> list[BenchmarkTask]`
- `entries(partition: BenchmarkPartition) -> list[BenchmarkPartitionEntry]`
- `entry_for(task_id: str, partition: str | None = None) -> BenchmarkPartitionEntry`
- `task_for(entry_or_task_id: str) -> BenchmarkTask`
- `verifiers_for(entry_or_task_id: str) -> list[VerifierSpec]`
- `fixtures_for(entry_or_task_id: str) -> list[BenchmarkFixtureRef]`
- `proxy_tasks_for_scope(scope: Sequence[str]) -> list[BenchmarkTask]`
- `representative_family_tasks(family: str, partition: str = "train", limit: int = 4) -> list[BenchmarkTask]`
- `task_family_map(partition: str) -> dict[str, str]`
- `objective_specs(partition: str = "train") -> list[ObjectiveSpec]`
- `smoke_task() -> BenchmarkTask`

Evaluation, archive objective construction, proxy selection, validation, full-train batching, and held-out reporting must use this registry. Direct `suite.train`, `suite.proxy`, `suite.val`, and `suite.test` reads become compatibility-only code.

### Registry Validation

Registry construction fails closed if:

- duplicate `(partition, task_id)` entries exist
- a selected `task_id` is absent from `benchmark_suite.json`
- entry `family` disagrees with the suite task family
- entry `adapter_kind` disagrees with the registered adapter mapping
- a `verifier_id` is absent from `VerifierBundle`
- a required fixture is missing from the fixture catalog
- `repo_patch`, `service_task`, or `browser_task` entries have empty environment digests
- compatibility projection fields disagree with `partition_entries`
- `benchmark_provenance.plan_id`, `suite_id`, or `verifier_bundle_id` mismatches loaded artifacts
- the same source task or fixture appears across train and held-out partitions without an explicit contamination record
- selected family pressure violates the WS4 minimums while library coverage exists

### Planning Flow

Replace the current authoritative `goal_conditioned_demo_clone` path with `build_goal_scoped_benchmark_plan(...)` in `agintor/runtime_builder.py`.

Flow:

1. Build or load the benchmark suite.
2. Score all families.
3. Select every family with score `>= 2`.
4. If none meet threshold, select the top two positive-scoring families.
5. If none are positive, default to `["e2e", "top"]`.
6. Force-include `e2e` for export, deployment, verification, workflow completion, orchestration, or composite-report goals.
7. Select at least two train tasks per selected family when available.
8. Select at least one proxy task per selected family when available.
9. Trigger bounded template synthesis only for uncovered capabilities or criteria.
10. Create `BenchmarkPartitionEntry` objects.
11. Fill compatibility task-id lists from entries.
12. Build `VerifierBundle` from entries.
13. Write `benchmark_plan.json`, `benchmark_suite.json`, `fixture_catalog.json`, `verifier_bundle.json`, and `benchmark_provenance.json`.
14. Reload those artifacts from disk.
15. Build `PlanScopedTaskRegistry`.
16. Pass the registry into `RuntimeEvaluator`, `EvolutionEngine`, and archive objective construction.

`build_goal_conditioned_suite()` can remain as a compatibility shim for existing tests and demo behavior, but it must not be the authoritative build-runtime path.

## 2. Benchmark Adapters And Fixtures

### Adapter Protocol

Add `BenchmarkAdapter` in `agintor/benchmarks.py`:

```python
class BenchmarkAdapter(Protocol):
    adapter_kind: BenchmarkAdapterKind
    fixture_model: type[BaseModel]

    def validate_fixture(self, fixture: BaseModel) -> None: ...
    def environment_digest(self, fixture: BaseModel) -> str: ...
    def hydrate_task(
        self,
        entry: BenchmarkPartitionEntry,
        raw_task: BenchmarkTask,
        fixture: BaseModel,
        context: BenchmarkAdapterContext,
    ) -> HydratedBenchmarkTask: ...
    def verifier_specs(
        self,
        entry: BenchmarkPartitionEntry,
        fixture: BaseModel,
    ) -> list[VerifierSpec]: ...
```

Add `BenchmarkAdapterContext`:

- `suite_name: str`
- `partition: BenchmarkPartition`
- `entry: BenchmarkPartitionEntry`
- `fixture_store_root: Path`
- `runtime_workspace_rel_root: str`
- `evaluation_workspace: Path`

Add `HydratedBenchmarkTask`:

- `task: BenchmarkTask`
- `fixture_refs: list[BenchmarkFixtureRef]`
- `verifier_ids: list[str]`
- `environment_digest: str`
- `setup_manifest: dict[str, Any]`

### Fixture Catalog

Add `BenchmarkFixtureCatalog`:

- `catalog_id: str`
- `schema_version: str`
- `fixtures: dict[str, BenchmarkFixtureRef]`
- `catalog_digest: str`

Digest inputs include normalized fixture JSON, relevant fixture file contents, declared command lists, verifier IDs, adapter kind, and runtime-relevant environment contracts. Digests must not include absolute checkout paths.

### Structured Ops

`StructuredOpsFixture` wraps existing demo task data:

- `fixture_id`
- `operation_inputs`
- `expected`
- `verifier_type`
- `output_schema`
- `local_judgeable_reason`

Hydration preserves current `BenchmarkTask.operations`, `expected`, and `verifier_type`, while stamping:

- `adapter_kind = "structured_ops"`
- `fixture_ids`
- `environment_digest`
- `artifact_contract`

### Repo Patch Lane

Add `RepoPatchFixture`:

- `fixture_id`
- `repository_snapshot`
  - `snapshot_id`
  - `snapshot_digest`
  - `root_artifact_id`
  - `files: list[{path, content_digest, executable}]`
- `writable_targets: list[str]`
- `read_only_paths: list[str]`
- `prompt: str`
- `patch_contract`
  - `response_format = "repo_patch_json_files_v1"`
  - `allowed_file_modes`
  - `max_files_changed`
  - `allow_create`
  - `allow_delete = False`
- `commands: list[RepoPatchCommand]`
  - `command_id`
  - `argv`
  - `cwd`
  - `timeout_s`
  - `expected_exit_code`
  - `stdout_contains`
  - `stderr_not_contains`
- `expected_outputs`
  - `file_contains`
  - `file_not_contains`
  - `json_artifact_shape`
  - `diff_constraints`
- `environment`
  - `python_version`
  - `platform_policy`
  - `env_allowlist`
  - `dependency_lock_digest`
- `isolation`
  - `copy_strategy = "copytree_from_fixture"`
  - `network_policy = "none"`
  - `filesystem_policy = "workspace-read-write"`
  - `path_policy = "fixture_relative_only"`

Hydration:

- materializes the snapshot into an evaluation/runtime workspace, not into the source checkout
- converts writable targets into runtime-workspace-relative paths
- creates a normal `BenchmarkTask` with `task_type = "repo_patch"`
- sets `allowed_tool_categories = ["filesystem/read", "filesystem/patch"]`
- sets task file paths to declared writable targets
- stores fixture manifest refs in context metadata, not raw host paths
- emits an `OperationSpec(kind="repo_patch", args={"target_file_paths": writable_targets})`
- sets `verifier_type = "repo_patch_suite"`

Repo patch verifier refs:

- `verifier.repo_patch.applicable.v1`
- `verifier.repo_patch.diff_shape.v1`
- `verifier.repo_patch.command_suite.v1`
- `verifier.repo_patch.expected_outputs.v1`
- `verifier.repo_patch.environment_digest.v1`

Fail closed if runtime output writes outside writable targets, omits required files, changes read-only files, produces invalid patch payloads, command exits mismatch, or environment digests mismatch.

### Service Task Lane

Add `ServiceTaskFixture`:

- `fixture_id`
- `initial_state`
  - typed JSON object
  - `state_digest`
- `service_model`
  - `transport = "http"`
  - `base_url_policy = "local_fixture_only"`
  - `routes: list[ServiceRouteSpec]`
- `transitions`
  - `transition_id`
  - `method`
  - `path`
  - `request_schema`
  - `preconditions`
  - `state_update`
  - `response`
- `allowed_actions`
  - method/path/body allowlist
- `transport_policy`
  - `network_policy = "local_loopback_only"`
  - `timeout_s`
  - `max_calls`
  - `forbid_external_hosts = True`
- `state_verifier_hooks`
  - `hook_id`
  - `expected_state`
  - `json_path_assertions`
  - `receipt_assertions`

Hydration:

- creates a deterministic local service fixture before the evaluation run
- exposes only a loopback URL in task args
- records the service manifest artifact ID
- creates a normal `BenchmarkTask` with `task_type = "service_task"`
- sets `allowed_tool_categories = ["service/http"]`
- emits `OperationSpec(kind="service_action", args={...})`
- sets `verifier_type = "service_state_transition"`

Service verifier refs:

- `verifier.service.allowed_transition.v1`
- `verifier.service.final_state.v1`
- `verifier.service.receipt_sequence.v1`
- `verifier.service.transport_policy.v1`

Failure modes:

- external URL
- non-loopback URL
- unsupported method
- undeclared route
- extra service call
- transition precondition failure
- final state mismatch
- missing or unreconciled receipt
- timeout
- nondeterministic response

### Browser And Multimodal

`BrowserTaskFixture` is scaffold-only for the MVP:

- local HTML fixture artifact
- declared DOM assertions
- screenshot/log refs
- no MVP gating claim

`MultimodalTaskFixture` is placeholder-only:

- serializable metadata
- skipped verifier
- rejected from train/proxy gating partitions unless marked `non_gating = True`

## 3. Verifier Catalog And Evidence

### VerifierSpec

Evolve `VerifierSpec` in `agintor/schemas.py`:

- `verifier_id`
- `family`
  - `exact`
  - `near_exact`
  - `repo_patch_applicability`
  - `repository_test_execution`
  - `diff_shape_constraints`
  - `service_state_transition`
  - `artifact_schema`
  - `milestone`
  - `browser_assertion`
- `task_id`
- `partition_entry_id`
- `adapter_kind`
- `fixture_ids`
- `required: bool`
- `score_weight: float`
- `input_artifact_contract`
- `expected`
- `params`
- `replay_policy = "disk_only"`
- `mutation_visibility = "hidden"`
- `created_from`

Verifier configs are typed Pydantic models. Provider-authored grader code is not allowed.

Extend `VerifierBundle`:

- `entry_verifier_bindings: dict[str, list[str]]`
- `fixture_bindings: dict[str, list[str]]`
- `created_from`
- `frozen = True`

Every `BenchmarkPartitionEntry` must have at least one bound verifier unless explicitly `non_gating`.

### Evidence Record

Add `VerifierEvidenceRecord`:

- `evidence_id`
- `evidence_schema_version`
- `verifier_id`
- `bundle_id`
- `plan_id`
- `partition_entry_id`
- `task_id`
- `partition`
- `adapter_kind`
- `runtime_hash`
- `candidate_id`
- `evaluation_stage`
- `seed`
- `request_id`
- `evaluation_unit_id`
- `run_id`
- `attempt_id`
- `run_root_ref`
- `score`
- `passed`
- `required`
- `failure_kind`
- `failure_reason`
- `rerun_eligibility`
- `deterministic`
- `artifact_refs`
- `receipt_refs`
- `runtime_event_refs`
- `grouped_trace_refs`
- `checkpoint_ref`
- `latest_checkpoint_ref`
- `environment_fingerprint_id`
- `verifier_input_digest`
- `fixture_digest`
- `evidence_payload_digest`
- `supporting_payload_ref`
- `mutation_visible = False`
- `prompt_visibility = "reporting_only"`
- `created_at`

Evidence payload models:

- `ExactEvidencePayload`
- `NearExactEvidencePayload`
- `RepoPatchApplicabilityEvidencePayload`
- `RepositoryTestExecutionEvidencePayload`
- `DiffShapeConstraintEvidencePayload`
- `ServiceStateTransitionEvidencePayload`
- `ArtifactSchemaEvidencePayload`
- `MilestoneEvidencePayload`
- `BrowserAssertionEvidencePayload`

### Verifier Runtime

Add in `agintor/verifiers.py`:

- `VerifierCatalog`
  - `register(family, runner)`
  - `get(verifier_id)`
  - `validate_bundle(bundle, registry)`
- `VerifierRunner`
  - `evaluate(spec, run_result, fixture, lineage) -> VerifierEvidenceRecord`
- `VerifierEvidenceStore`
  - `write(record) -> str`
  - `load(evidence_ref) -> VerifierEvidenceRecord`
  - `write_index(records) -> str`
- `replay_verifier_evidence(evidence_ref, bundle, registry) -> VerifierReplayResult`

Replay is disk-only. It may recompute exact comparisons from persisted artifacts and evidence payloads. It must not call `RuntimeHost`, provider APIs, mutation code, internet services, or runtime execution.

Repository test evidence replay validates recorded command outputs and digests. A separate future reproduce mode may rerun local tests, but replay does not.

### Failure Taxonomy

Add `VerifierFailureKind`:

- `plan_binding_missing`
- `fixture_missing`
- `fixture_digest_mismatch`
- `verifier_config_invalid`
- `runtime_protocol_failure`
- `artifact_missing`
- `artifact_schema_mismatch`
- `exact_mismatch`
- `near_exact_mismatch`
- `patch_apply_failed`
- `patch_out_of_scope`
- `repo_tests_failed`
- `repo_test_timeout`
- `service_transition_mismatch`
- `service_fixture_dirty`
- `milestone_missing`
- `browser_assertion_failed`
- `browser_fixture_unavailable`
- `evidence_replay_mismatch`
- `environment_mismatch`
- `checkpoint_unavailable`
- `grouped_trace_unavailable`

Every failure row should include severity, deterministic status, rerun eligibility, user-visible reason, and optional debug payload ref.

## 4. Evaluation Integration

`RuntimeEvaluator` should accept `PlanScopedTaskRegistry`, `VerifierCatalog`, `VerifierEvidenceStore`, and the frozen artifact paths.

Compatibility constructor:

- `RuntimeEvaluator.from_legacy_suite(...)` can exist only for migration tests.

Replace:

- `suite.all_tasks(partition)` with `registry.tasks(partition)`
- direct proxy lists with `registry.proxy_tasks_for_scope(scope)`
- smoke task fallback with `registry.smoke_task()`
- suite-based objective subsets with registry objective specs
- direct verifier-type scoring with `VerifierCatalog` scoring

Add `EvaluationRunRecord`:

- `partition_entry_id`
- `task_id`
- `seed`
- `runtime_hash`
- `run_result_ref`
- `run_id`
- `attempt_id`
- `request_id`
- `evaluation_unit_id`
- `checkpoint_ref`
- `latest_checkpoint_ref`
- `recovery_attempt_refs`
- `receipt_refs`
- `runtime_event_refs`
- `grouped_trace_refs`
- `verifier_refs`
- `verifier_evidence_refs`
- `score`
- `passed`
- `failure_kind`

Extend `SuiteEvaluation`:

- `evaluation_id`
- `plan_id`
- `verifier_bundle_id`
- `partition_entry_ids`
- `evaluation_run_records`
- `verifier_evidence_index_ref`
- `grouped_trace_refs`
- `checkpoint_refs`

Stage-failure rows become flat rows, not nested blobs:

- `stage`
- `iteration`
- `objective`
- `candidate_hash`
- `parent_hash`
- `touched_scope`
- `operator_type`
- `decision_id`
- `benchmark_refs`
- `verifier_refs`
- `verifier_evidence_refs`
- `evaluation_unit_id`
- `request_id`
- `task_id`
- `seed`
- `run_id`
- `attempt_id`
- `checkpoint_ref`
- `recovery_attempt_refs`
- `grouped_trace_refs`
- `failure_kind`
- `failure_reason`
- `rerun_eligibility`

## 5. Search State And Resume

### State Models

Add in `agintor/schemas.py`:

- `SearchState`
- `SearchResumeManifest`
- `SearchArchiveState`
- `ScopeSchedulerState`
- `ObjectiveSelectorState`
- `OperatorPortfolioState`
- `OperatorArmState`
- `SearchLineageRecord`
- `ValidationCursorState`
- `PhaseBudgetState`
- `PredictorSnapshotRef`

Add helpers:

- `QualityDiversityArchive.to_state()`
- `QualityDiversityArchive.from_state(...)`
- `ScopeScheduler.to_state()`
- `ScopeScheduler.from_state(...)`
- `DecisionFamilyModelBank.snapshot()`
- `DecisionFamilyModelBank.restore_snapshot(...)`

Add `SearchStateManager` in `agintor/evolution.py` or new `agintor/search_state.py`:

- `write_state(engine) -> SearchState`
- `write_resume_manifest(engine, state) -> SearchResumeManifest`
- `load_for_resume(evolution_dir, current_inputs) -> SearchResumeBundle`
- `restore_engine(bundle, engine) -> None`

### search_state.json

`search_state.json` stores:

- schema and state IDs
- current iteration and next iteration
- requested and completed steps
- archive cells, runtime dirs, runtime descriptors, and evaluation refs
- scheduler phase, credits, stagnation, need, hard-failure rates
- objective selector policy, selection counts, score history
- operator portfolio arm state and history
- RNG state and draw counters
- active predictor snapshot ref and digest
- phase budgets
- stage counters
- validation cursor
- leader set
- lineage rows
- refs to history, validation history, stage failures, archive index

Do not inline full `SuiteEvaluation` payloads. Store them under `evolution/evaluations/*.json` and reference them by runtime hash.

### search_resume_manifest.json

`search_resume_manifest.json` stores digests and IDs for:

- `benchmark_plan.json`
- `verifier_bundle.json`
- `benchmark_provenance.json`
- `benchmark_suite.json`
- `fixture_catalog.json`
- baseline runtime dir, hash, code hash
- runtime contract version
- deployment contract digest
- kernel manifest digest
- kernel files digest
- runtime identity inputs digest
- runtime profile digest
- state-store schema version
- checkpoint envelope schema version
- runtime backend
- provider
- mutator type
- artifact mode
- workspace root
- Python requirement
- completed iteration and next iteration
- active predictor snapshot ref
- validation cursor

Use the existing lightweight runtime contract version as ABI identity. Do not introduce a new version-axis system.

### Resume Algorithm

`SearchStateManager.load_for_resume(evolution_dir, current_inputs)`:

1. Load manifest and state with strict Pydantic models and `extra="forbid"`.
2. Recompute file digests for frozen planning artifacts.
3. Load baseline runtime through `load_runtime(...)`.
4. Compare runtime hash, code hash, runtime contract version, deployment contract digest, kernel manifest digest, kernel files digest, runtime identity input digest, and runtime profile digest.
5. Compare `STATE_STORE_SCHEMA_VERSION` and `CHECKPOINT_ENVELOPE_SCHEMA_VERSION`.
6. Verify every archive runtime dir exists and loads to the recorded runtime hash.
7. Fail closed if an accepted elite, leader, validation cursor runtime, or archive runtime is missing.
8. Ignore missing rejected candidate artifacts only when no state or lineage record depends on them.
9. Load predictor snapshot ref and compare digest.
10. Restore archive, scheduler, objective selector, operator portfolio, phase budgets, counters, validation cursor, RNG state, and leaders.
11. Resume at `state.next_iteration`.
12. Never rerun the completed iteration unless a future explicit replay option is added.

On mismatch, raise `SearchResumeError` with key, expected value, and actual value. Do not regenerate suites, reseed archives, or silently rebuild planning artifacts.

### CLI

`agintor evolve`:

- add `--resume-from <evolution_dir>`
- add `--no-resume`
- in resume mode, `--steps N` means run N additional iterations
- JSON output includes `search_state_path`, `search_resume_manifest_path`, and `resumed_from_iteration`

`agintor build-runtime`:

- add `--resume-from <build_workspace>/evolution`
- only resume if the same build workspace planning artifacts and seed runtime match
- do not resume across a different factory chat, prompt, follow-up, profile, destination, or runtime identity

## 6. Objective And Operator Policy

### ObjectiveSelector

Replace random objective selection with `ObjectiveSelector.choose(objectives, archive, scheduler, predictors, history)`.

Score:

```text
score =
  0.35 * archive_undercoverage
+ 0.20 * family_underrepresentation
+ 0.20 * uncertainty
+ 0.15 * stagnation_age
- 0.10 * recent_acceptance_rate
```

Tie-breaker:

```text
score desc, last_selected_iteration asc, objective.name asc
```

Inputs:

- archive undercoverage: inverse count of archive cells for objective
- family underrepresentation: plan-selected family balance
- uncertainty: predictor uncertainty when available, else 0
- stagnation age: iterations since accepted improvement
- recent acceptance rate: rolling last 20 attempts

Persist every choice in `objective_choices.json`.

### OperatorPortfolio

Add `OperatorPortfolio.choose(...)` before mutation and crossover.

Arms:

- `heuristic_mutation`
- `provider_mutation`
- `crossover`
- `simplification`

Score:

```text
utility =
  mean_delta
+ 0.25 * stage4_pass_rate
+ 0.20 * acceptance_rate
- 0.25 * hard_failure_rate
+ exploration_bonus
- cooldown_penalty
```

```text
exploration_bonus = sqrt(log(total_attempts + 1) / (arm_attempts + 1)) * 0.05
```

Tie-breaker:

```text
utility desc, least_recently_used asc, operator name asc
```

Persist every choice in `operator_choices.json`. Crossover becomes an operator arm, not a hidden probabilistic branch.

### Lineage

Add `SearchLineageRecord`:

- iteration
- objective
- scope
- operator
- operator reason
- parent runtime hash and dir
- donor runtime hashes
- candidate patch ref and digest
- child runtime hash, dir, and code hash
- stage result ref
- archive inserted keys
- accepted or rejected reason
- request IDs
- evaluation-unit IDs
- task IDs
- seeds
- run IDs
- attempt IDs
- latest checkpoint refs
- recovery attempt refs
- grouped trace refs
- predictor snapshot before and after refs
- RNG draw counters before and after

## 7. Signal Sufficiency

Add `agintor/evaluation_signal.py`.

Models:

- `StageSignalCounters`
- `SignalThresholds`
- `FullTrainEvidenceSummary`
- `GateDecision`
- `SignalSufficiencyReport`
- `SignalGatePolicy`
- `SignalAccounting`

### signal_sufficiency.json

Required sections:

- schema version
- plan ID
- verifier bundle ID
- benchmark provenance ID
- search state ID
- iteration window
- stage counters for Stage 0 through Stage 4
- completed full-train candidates
- accepted elites
- task-seed rows
- unique task IDs
- unique families
- unique scopes
- adapter counts
- thresholds
- gates:
  - `archive_insertion`
  - `scheduler_credit`
  - `predictor_retraining`
  - `ws5_runtime_control`
- recommended policy
- blocking reasons

Thresholds from `compute_signal_thresholds(plan, scheduler)`:

```text
min_stage4_candidates = max(20, 4 * selected_family_count, 2 * active_scope_count)
min_accepted_elites = max(6, selected_family_count + active_scope_count)
min_task_seed_rows = max(120, 15 * selected_family_count, 3 * train_task_count * full_train_seed_count)
min_per_selected_family_rows = max(15, 3 * full_train_seed_count)
min_predictor_positive_labels = 10
min_predictor_negative_labels = 10
min_ws5_stage4_candidates = max(50, 10 * selected_family_count, 5 * active_scope_count)
```

### Stage Gates

Stage 0:

- patch applies
- parses
- stays inside mutable contracts
- failure increments hard-failure rate only
- no archive, scheduler credit, or predictor observation

Stage 1:

- deterministic smoke
- pass allows proxy evaluation
- failure increments hard-failure rate
- no scheduler credit

Stage 2:

- touched-scope proxy
- pass allows Stage 3
- updates proxy diagnostics only
- no archive or predictor training

Stage 3:

- local train subset
- pass allows Stage 4
- can influence stage tightening
- no archive or scheduler credit

Stage 4:

- full train over frozen plan train entries and full-train seeds
- only complete non-invalid Stage 4 results may insert into archive
- only complete Stage 4 deltas may update scheduler credit
- predictor retraining uses Stage 4 label diversity gates

### Insufficient Signal Policy

`recommend_signal_policy(report)` returns:

- `tighten_scope` when Stage 0 pass rate is below 0.35 or Stage 1 pass rate is below 0.70
- `expand_proxy` when Stage 2 pass rate is below 0.20 or Stage 4 volume is below threshold while Stage 1 is healthy
- `simplify_objective` when Stage 3 pass rate is below 0.10 or global objectives dominate failures
- `block_claim` when held-out report is invalid, mutation leakage is detected, Stage 4 evidence is below threshold at campaign end, or WS5 predictor gates fail

## 8. Held-Out Reports And Contamination

### HeldOutReport

Add `HeldOutReport`, `HeldOutPartitionReport`, and `HeldOutClaimDecision`.

Persist as `evolution/held_out_report.json` or `build/held_out_report.json`.

Required fields:

- `report_id`
- `plan_id`
- `verifier_bundle_id`
- `benchmark_provenance_id`
- `runtime_hash`
- `runtime_profile_hash`
- `search_state_digest`
- validation partition report
- test partition report
- task IDs
- seeds
- fixture IDs
- verifier IDs
- environment digests
- run IDs
- attempt IDs
- checkpoint refs
- grouped trace refs
- score rows
- train reference score
- validation score
- test score
- robustness
- cost
- latency
- fault rate
- verifier coverage
- claim decision

Invalidation rules:

- fail closed on plan, verifier bundle, provenance, suite, runtime hash, runtime profile, backend, or fixture digest mismatch
- invalidate test claims if test task/run/verifier evidence appears in mutation context, predictor training, archive insertion, scheduler credit, or objective selection before final reporting
- invalidate validation claims if validation traces or verifier outputs appear in mutation prompts
- warn on verifier-shape overlap alone
- hard-invalidate when verifier-shape overlap combines with source, template, or fixture overlap across train/test

### ContaminationRecord

Add `ContaminationRecord` and `ContaminationManifest`.

Kinds:

- `source_overlap`
- `template_overlap`
- `fixture_overlap`
- `verifier_shape_overlap`
- `provider_assisted_proposal`
- `mutation_context_leakage`

Fields:

- `record_id`
- `kind`
- `severity`
- `subject_partition`
- `subject_task_id`
- `comparison_partition`
- `comparison_task_id`
- `source_task_id`
- `template_id`
- `fixture_ids`
- `environment_digest`
- `verifier_shape_digest`
- `overlap_score`
- `evidence_refs`
- `decision`

Hard invalidation:

- same source task across train and test
- same serious-lane fixture or environment digest across train and test
- provider-assisted proposal introduces unknown task/template/grader
- validation/test trace refs, transcripts, verifier evidence, task IDs, expected outputs, or grouped traces enter mutation prompt

## 9. Mutation Prompt Filtering

Replace raw failure traces in mutation prompts with `MutationPromptEvidence`.

Add:

- `build_mutation_evidence(...)`
- `filter_mutation_prompt_evidence(...)`
- `assert_mutation_context_clean(...)`

Allowed:

- objective
- touched scope
- mutable policy files
- method contracts
- predictor summaries with trace refs stripped
- train/proxy failure summaries as counts, failure kinds, task family, and score bucket
- exemplar runtime hash, score, and scope

Forbidden:

- validation task IDs
- test task IDs
- validation/test expected outputs
- raw traces
- grouped transcripts
- verifier outputs
- held-out report rows
- checkpoint payloads containing held-out evidence
- missing provenance

`build_mutation_prompt()` should accept `MutationPromptEvidence`, not arbitrary trace dictionaries.

## 10. Factory Trace Stamping

Add `agintor/factory_trace.py` or keep the helper in `agintor/runtime_builder.py`.

Add:

```python
FactoryTracePurpose = Literal[
    "planning_refine",
    "mutation_patch",
    "patch_repair",
    "objective_choice",
    "operator_choice",
]
```

Extend `OpenAITraceContext` with optional factory fields:

- `factory_action`
- `operator_type`
- `decision_id`
- `decision_reason`

Helper:

```python
derive_factory_trace_context(
    parent,
    *,
    build_id: str,
    purpose: FactoryTracePurpose,
    iteration: int | None = None,
    objective: str | None = None,
    touched_scope: Sequence[str] = (),
    runtime_hash: str | None = None,
    runtime_dir: str | None = None,
    operator_type: str | None = None,
    decision_id: str | None = None,
    decision_reason: str | None = None,
) -> OpenAITraceContext
```

Call sites:

- `_maybe_provider_refine_planning(...)`: `factory_action = "planning_refine"`
- `EvolutionEngine._select_objective(...)`: returns `ObjectiveDecision` and records `objective_choices.json`
- `OperatorPortfolio.choose(...)`: records `operator_choices.json`
- `ProviderPatchMutator.mutate(...)`: `factory_action = "mutation_patch"`
- patch repair path: `factory_action = "patch_repair"`

Provider adapters preserve factory trace context. They do not infer provider role or synthesize missing fields.

## 11. Reporting And Summary Fields

Upgrade reports:

`validation_history.json`:

- `validation_id`
- `iteration`
- `candidate_hash`
- `runtime_dir`
- `benchmark_plan_id`
- `verifier_bundle_id`
- `benchmark_provenance_id`
- `partition = "val"`
- seeds
- scores by family
- run IDs
- attempt IDs
- checkpoint refs
- grouped trace refs

`stage_failures.json`:

- flat rows, one failed stage per row
- include decision IDs, runtime lineage, verifier refs, evidence refs, and rerun eligibility

`leaderboard.json`:

- `leaderboard_id`
- `selection_policy`
- `benchmark_plan_id`
- `held_out_report_id`
- train score
- validation score
- held-out score
- robustness
- cost
- latency
- fault rate
- verifier coverage

`evolution_history.json`:

- `objective_decision_id`
- `operator_decision_id`
- `operator_type`
- `mutation_trace_call_id`
- `repair_trace_call_id`
- `accepted_reason`
- `rejected_reason`

`archive_index.json`:

- artifact refs
- runtime dir
- objective
- cell key
- plan IDs
- trace refs
- validation refs
- held-out refs

Add summary fields:

`BuildSummary`:

- `benchmark_provenance_path`
- `held_out_report_path`
- `search_state_path`
- `search_resume_manifest_path`
- `signal_sufficiency_path`
- `proof_campaign_path`

`EvolutionSummary`:

- `search_state_path`
- `search_resume_manifest_path`
- `signal_sufficiency_path`
- `held_out_report_path`
- `objective_choices_path`
- `operator_choices_path`

`ExportSummary`:

- `benchmark_plan_path`
- `verifier_bundle_path`
- `benchmark_provenance_path`
- `held_out_report_path`
- `proof_campaign_path`
- `source_build_summary_path`

CLI JSON output for `build-runtime` and `evolve` should include:

```json
{
  "reports": {
    "benchmark_provenance": "...",
    "search_state": "...",
    "search_resume_manifest": "...",
    "signal_sufficiency": "...",
    "held_out_report": "...",
    "proof_campaign": "..."
  }
}
```

## 12. Proof Campaign

Add `ProofCampaignSpec` and emit `proof_campaign.json`.

Minimum MVP campaign:

- suites:
  - `serious_repo_patch_v1`
  - `serious_service_task_v1`
  - `structured_ops_v1` sanity baseline
- train seeds: `0,1,2`
- proxy seeds: `0,1`
- validation seeds: `10,11,12`
- held-out seeds: `100,101,102,103,104`
- at least 3 independent factory runs per serious lane
- 20 evolution steps minimum for local smoke
- 100 evolution steps recommended for proof runs

Pass criteria:

- no frozen-artifact mismatch
- no mutation exposure to validation/test traces
- held-out report present
- serious-lane verifier coverage `>= 0.95`
- no unresolved Stage 0 integrity failures in exported leader
- search resume manifest validates
- default tests remain offline and deterministic

Minimum improvement bar:

- exported leader held-out global score improves by `>= 0.03` absolute over baseline, or
- one serious lane improves by `>= 0.05` without regressing global held-out by more than `0.01`

Example commands:

```powershell
agintor build-runtime "<goal>" --destination <dir> --steps 100 --suite serious_repo_patch_v1 --workspace <ws>
agintor evolve <runtime_dir> --suite serious_service_task_v1 --steps 100 --workspace <ws>
agintor eval <leader_runtime> --suite serious_repo_patch_v1 --partition test --seeds 100,101,102,103,104
```

Report IDs:

- `proof.<stable_hash(campaign_spec, benchmark_plan_id, verifier_bundle_id, baseline_hash)>`
- `heldout.<proof_id>.<runtime_hash>`

## 13. Implementation Sequence

1. Add core schemas.
   - `BenchmarkPartitionEntry`
   - `BenchmarkFixtureRef`
   - `BenchmarkProvenance`
   - verifier evidence models
   - search-state models
   - signal and held-out models

2. Build plan-scoped registry.
   - registry API in `agintor/benchmarks.py`
   - legacy plan migration helper
   - objective specs from registry
   - fail-closed registry validation

3. Add fixture catalog and adapters.
   - structured ops wrapper
   - repo patch fixture and hydration
   - service task fixture and hydration
   - browser scaffold
   - multimodal placeholder

4. Add verifier catalog and evidence store.
   - typed verifier families
   - disk-only replay
   - evidence index
   - failure taxonomy

5. Rewire evaluator.
   - consume registry
   - execute through `RuntimeHost`
   - score through verifier catalog
   - persist evidence and flat stage failures

6. Rewire runtime builder.
   - write benchmark suite, fixture catalog, plan, provenance, verifier bundle
   - reload artifacts from disk
   - pass registry into evaluator and evolution
   - surface new summary paths

7. Implement search persistence.
   - archive and scheduler state helpers
   - predictor snapshots
   - search state manager
   - resume manifest validation
   - CLI resume flags

8. Implement deterministic search policy.
   - objective selector
   - operator portfolio
   - simplification operator hook
   - objective and operator choice ledgers

9. Implement signal and contamination controls.
   - signal sufficiency report
   - held-out report
   - contamination manifest
   - mutation prompt filtering

10. Implement proof campaign and release gates.
    - serious repo patch suite
    - serious service task suite
    - proof campaign JSON
    - CLI evidence paths

## 14. Test Plan

Add `tests/test_benchmark_registry.py`:

- resolves entries from frozen suite, verifier bundle, provenance, and fixtures
- rejects duplicate partition entries
- rejects missing task, verifier, or fixture refs
- preserves adapter kind and family separation
- rejects projection mismatch between task-id lists and entries
- flags train/held-out contamination

Add `tests/test_benchmark_adapters.py`:

- rejects duplicate adapter kind
- rejects unknown adapter kind
- validates built-in fixture models
- proves hydration is deterministic and digest-stable

Add `tests/test_benchmark_fixture_catalog.py`:

- catalog IDs match content digests
- fixture absolute paths are rejected
- repo snapshots cannot escape fixture root
- environment digests ignore absolute checkout paths

Add `tests/test_repo_patch_benchmark_adapter.py`:

- golden fixture hydrates to `BenchmarkTask(task_type="repo_patch")`
- compiled plan contains `repo_patch` node with declared target files
- undeclared writable targets are rejected
- command verifier passes and fails deterministically
- read-only path modification fails closed

Add `tests/test_service_task_benchmark_adapter.py`:

- golden fixture hydrates to `service_action`
- non-loopback URL is rejected
- invalid transition is rejected
- final-state hook evidence serializes
- extra service call fails closed

Add `tests/test_verifier_catalog.py`:

- typed verifier bundle validation
- exact and near-exact evidence replay
- repo patch applicability evidence
- repository test execution evidence from recorded outputs
- diff-shape violations
- service-state transition evidence
- artifact-schema JSON pointer errors
- milestone refs
- browser assertion scaffold serialization

Add `tests/test_evaluator.py` or extend existing evaluator tests:

- evaluator uses partition entries, not full suite lists
- stage selection uses registry-selected tasks only
- verifier evidence index is persisted
- replay from frozen disk artifacts makes zero provider calls
- validation and test evidence never enter mutation context
- stage-failure rows include taxonomy and rerun eligibility

Add `tests/test_search_resume.py`:

- search state round-trips archive, scheduler, predictors, RNG
- manifest fails closed on benchmark plan digest mismatch
- manifest fails closed on verifier bundle digest mismatch
- manifest fails closed on benchmark provenance digest mismatch
- manifest fails closed on suite digest mismatch
- manifest fails closed on runtime contract mismatch
- manifest fails closed on kernel manifest digest mismatch
- manifest fails closed on state-store schema mismatch
- resume restores next iteration without reseeding archive
- missing archive runtime dir blocks resume
- missing rejected candidate artifact does not block resume when unreferenced

Add `tests/test_evaluation_signal.py`:

- counts Stage 0 through Stage 4
- computes thresholds
- archive insert requires complete valid Stage 4 full-train result
- scheduler credit uses only complete Stage 4 deltas
- predictor retraining requires Stage 4 label diversity
- WS5 predictor control blocks without valid held-out report

Add `tests/test_held_out_reporting.py`:

- held-out report fails closed on plan or verifier digest mismatch
- report invalidates when test evidence is used before final reporting
- leader export references valid held-out report

Add `tests/test_contamination_control.py`:

- source and fixture overlap hard-invalidate test claims
- verifier-shape overlap warns until combined with fixture overlap
- provider-assisted planning accepts known templates only
- mutation-context leakage invalidates candidate lineage

Add `tests/test_mutation_prompt_filtering.py`:

- predictor summaries strip trace refs
- validation trace rows are rejected
- test verifier evidence is rejected
- sanitized train failure summaries are allowed

Add `tests/test_factory_trace_stamping.py`:

- planning refine context uses `provider_role="factory"`
- mutation context includes build ID, iteration, objective, touched scope, runtime hash
- patch repair derives from mutation context
- objective and operator choices get decision IDs

Add `tests/test_evolution_reporting.py`:

- flat `stage_failures.json`
- enriched `validation_history.json`
- enriched `leaderboard.json`
- enriched `archive_index.json`
- writes `objective_choices.json`
- writes `operator_choices.json`

Add or update `tests/test_cli.py`:

- `build-runtime` JSON includes `reports`
- `evolve` JSON includes `reports`
- `evolve --resume-from` runs additional steps and prints resume paths

## 15. Open-Source WS4 Release Gates

WS4 is not release-complete until:

- `BenchmarkPlan.partition_entries` is canonical
- `benchmark_provenance.json` is frozen, reloadable, and linked from summaries
- evaluation can rerun from frozen disk artifacts without raw-goal reparse
- a serious `repo_patch` lane exists with typed fixtures and local verifiers
- a serious `service_task` lane exists with typed fixtures and local verifiers
- verifier evidence replays from disk without provider calls
- `search_state.json` and `search_resume_manifest.json` exist and fail closed on mismatches
- `signal_sufficiency.json` gates archive, scheduler, predictor, and WS5 consumption
- `held_out_report.json` cites plan, verifier bundle, provenance, runtime hash, fixture digests, and run lineage
- validation and test evidence are provably excluded from mutation prompts
- CLI output exposes evidence paths
- default tests remain offline and deterministic

