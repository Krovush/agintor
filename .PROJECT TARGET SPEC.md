## Task

Retain and extend the existing repository structure where it already reflects the correct responsibility boundaries. Add new modules only when there is a real contract gap. Do not bury new functionality inside unrelated files merely to avoid creating a new module, and do not create ornamental abstraction layers that exist only to mirror section headings. The required abstraction layers in this document are responsibility layers and frozen data-contract layers, not instructions to explode the codebase into unnecessary wrappers.

The final Agintor MVP must be a usable CLI product, not just a library and not just an internal benchmark harness.

## Goal

The system must preserve the following ordered abstraction stack, and it must freeze a concrete structured artifact between adjacent layers so that signal is preserved without responsibility leakage:

1. **User goal and deployment intent.** Raw natural-language request plus any explicit user overrides.
2. **Goal interpretation and normalization.** Capability extraction, constraint extraction, deployment preference extraction, and success-criteria definition.
3. **Benchmark-family selection and/or goal-conditioned benchmark pressure synthesis.** Translation of the normalized goal into measurable pressure on topology, memory, tooling, end-to-end solving, and control efficiency.
4. **Verifier and grader generation or adaptation.** Local, deterministic scoring rules and checker ladders frozen before candidate evolution begins.
5. **Runtime-factory planning and profile resolution.** Resolution of factory-only settings, runtime-only settings, provider roles, backend choices, budget caps, and export ABI.
6. **Bounded runtime artifact contract.** Concrete definition of what the produced runtime consists of, what is immutable, what is mutable, and what will be exported.
7. **Fixed runtime host and immutable shell substrate.** The stable execution kernel that loads and runs candidate runtimes.
8. **Mutable runtime policy surface.** Topology, memory, tooling, and control decision logic that the outer loop is allowed to evolve.
9. **Task-time runtime execution state and agent orchestration.** Queue state, checkpoints, open handles, message board state, memory state, tool visibility, and solve-time verification requests.
10. **Staged evaluation, scoring, robustness measurement, and archive insertion.** Factory-side comparison of candidate runtimes under frozen benchmark pressure.
11. **Outer-loop evolution and leader selection.** Parent selection, mutation or crossover, staged acceptance, diversity preservation, validation, and export leader choice.
12. **Exported runtime solve and deploy path.** The user-facing produced MAS artifact and the CLI path that runs it after export.

These are the required abstraction layers. They are normative at the level of ownership and information flow. They are not instructions to hand-design every class hierarchy in advance. The coding agent may choose the minimum clean implementation that preserves these boundaries, but it must not collapse them into one undifferentiated system.

Later layers may consume earlier layers only through their frozen structured artifacts. A later layer must not silently reparse raw goal text or raw validation outputs when a normalized artifact already exists. The system must preserve the output of each stage and make it inspectable from the CLI workspace.

The resulting MVP must successfully demonstrate all core functionality through the CLI, including:

- goal intake,
- requirement normalization,
- capability and constraint extraction,
- success-criteria definition,
- benchmark-family selection and/or synthesis,
- verifier bundle freeze,
- runtime planning and seeded runtime creation,
- bounded runtime evolution,
- leader validation and export,
- produced-runtime solve execution,
- and deploy-path documentation for the exported runtime.

---

## 1. Product Definition and Scope

Agintor is a **runtime factory** for bounded multi-agent systems. It does not search over one benchmark-specific prompt. It searches over executable runtime policy code that decides:

- when to reuse or create agents,
- what to remember, summarize, retrieve, or promote,
- which tools to reuse or synthesize,
- which model class and check path to spend budget on,
- and when to stop.

The MVP is **CLI-first**. There is no GUI requirement. The CLI is the product surface. Intermediate build artifacts must be human-inspectable and machine-readable.

The MVP is intentionally bounded. “Build and evolve a functional and superior MAS of any kind” must be interpreted as:

> build and evolve any multi-agent runtime that can be expressed inside the Agintor runtime contract, tool permission model, verifier model, and deployment ABI.

It does **not** mean:

- arbitrary unsafe code execution,
- arbitrary internet toolchains,
- unconstrained autonomous benchmark invention with unverifiable scoring,
- arbitrary remote deployment infrastructure,
- or universal superiority on domains that the benchmark and verifier plan never measured.

The MVP must be honest about this boundary. It must still be highly capable inside that boundary.

The central architectural split is:

- **Agintor factory control plane** builds, evaluates, evolves, validates, and exports runtimes.
- **Runtime host / fixed shell** loads and runs exported runtimes.
- **Exported runtime artifact** is the produced MAS runtime configuration plus mutable policy code and related assets.
- **Mutable runtime policies** are only the solve-time decision surfaces. They are not allowed to own benchmark generation, archive logic, leader selection, or provider secret handling.

The MVP must prefer better runtimes than the seeded baseline under the chosen benchmark pressure, but it must not claim universal superiority outside the frozen evaluation domain.

---

## 2. Canonical Planes and Ownership

The clean separation below is mandatory.

| Plane | Owns | Must not own |
|---|---|---|
| **Agintor factory control plane** | goal normalization, success-criteria extraction, benchmark planning, verifier freeze, seed runtime materialization, mutation and crossover, staged evaluation, archive, validation, leader selection, export packaging, build reports | task-time agent reasoning, mutable runtime state, shell invariant bypass, provider secrets inside exports |
| **Runtime host / immutable shell substrate** | runtime loading, task execution loop, agent cloning, message board, checkpointing, short-term and long-term memory containers, tool registry and executor, sandboxing, safety guards, trace writing, request adaptation, invariant enforcement | goal planning, benchmark planning, archive logic, phase scheduling, leader selection |
| **Mutable runtime policy surface** | solve-time decision logic for topology, memory, tooling, and control | benchmark mutation, verifier mutation, archive mutation, phase scheduling, provider resolution, direct sandbox or filesystem bypass |
| **Benchmark and verifier bundle** | task adapters, deterministic grading, checker ladders, partition freeze, exact benchmark correctness definitions | candidate-specific adaptation after freeze, solve-time policy decisions |
| **Provider integration layer** | model request execution, local deterministic provider, hosted provider adapters, request accounting, container payload serialization, env resolution | benchmark semantics, archive semantics, export-time secret persistence |
| **Exported runtime artifact** | manifest, runtime profile, mutable policies, promoted tool metadata, provenance and export bundles, deployment contract | factory history, mutator prompts, validation and test traces, archive state, user secrets |

### 2.1 File and module ownership relative to the current repository

The existing repository already suggests most of the correct split. The coding agent should preserve that split and complete it instead of rewriting the project into a different architecture.

**Factory control plane modules to retain and extend:**

- `agintor/cli.py`
- `agintor/runtime_builder.py`
- `agintor/goal_rubric.py` or its replacement
- `agintor/benchmarks.py`
- `agintor/verifiers.py`
- `agintor/evaluator.py`
- `agintor/evolution.py`
- `agintor/archive.py`
- `agintor/mutator.py`
- `agintor/crossover.py`
- `agintor/prompt_builder.py`
- `agintor/prompts.py`
- `agintor/providers.py`, `provider_common.py`, `provider_openai.py`, `provider_minimax.py`
- `agintor/container_runtime.py`

**Runtime host and immutable shell modules to retain and extend:**

- `agintor/runtime_loader.py`
- `agintor/runtime_api.py`
- `agintor/runner.py`
- `agintor/shell.py`
- `agintor/memory_graph.py`
- `agintor/tool_runtime.py`
- `agintor/container_entry.py`

**Mutable runtime artifact files to preserve as the primary search surface:**

- `templates/baseline_runtime/topology_policy.py`
- `templates/baseline_runtime/memory_policy.py`
- `templates/baseline_runtime/tool_policy.py`
- `templates/baseline_runtime/control_policy.py`

**Artifact metadata files that remain central:**

- `runtime_manifest.json`
- `runtime_profile.json`
- `runtime_export_bundle.json`
- `runtime_provenance_bundle.json`

If additional modules are required, place them alongside the plane that owns the responsibility. Do not put factory-only planning inside runtime shell modules, and do not put runtime solve logic inside factory planning modules.

---

## 3. CLI Product Contract

The CLI must expose a complete user path. The golden path is `build-runtime`, but the produced runtime must also be runnable after export without re-entering the evolution pipeline.

### 3.1 Mandatory commands

The MVP must support the following command surfaces. Existing names should be preserved where practical.

#### `agintor build-runtime`

This is the end-to-end build path. It must:

1. accept a natural-language goal from an argument or file,
2. normalize the goal,
3. derive success criteria,
4. create or adapt a benchmark and verifier plan,
5. resolve factory and runtime profiles,
6. materialize a seed runtime,
7. run bounded evolution,
8. validate and select a leader,
9. export the produced runtime,
10. write a structured build summary and workspace artifacts,
11. print a final summary to stdout in JSON or JSON-compatible form.

This command must not require the user to manually author benchmark suites, runtime manifests, or mutation prompts for the golden path.

#### `agintor solve`

This command must support **both**:

1. **benchmark mode**, where the user runs an exported or seed runtime against a benchmark task by task ID and suite, and
2. **user-request mode**, where the user provides a natural-language prompt or request file and the exported runtime solves it through the runtime host.

The MVP must not force all post-export usage through benchmark task IDs only. The produced MAS must have an actual user-facing solve path.

#### `agintor eval`

This evaluates a runtime on a benchmark partition and seeds. It remains a factory/expert tool.

#### `agintor evolve`

This evolves an existing runtime directory against a suite. It remains a factory/expert tool.

#### `agintor init-runtime`

This materializes the seed runtime template and is allowed to remain an expert utility.

### 3.2 CLI output expectations

`build-runtime` must write a workspace with frozen intermediate artifacts and return a final structured summary containing at least:

- `goal_prompt`
- `goal_spec_path`
- `success_criteria_path`
- `benchmark_plan_path`
- `verifier_bundle_path`
- `runtime_plan_path`
- `output_runtime_dir`
- `workspace`
- `agintor_provider`
- `runtime_provider`
- `best_train_score`
- `best_goal_score`
- `best_val_score`
- `archive_cells`
- `accepted_mutations`
- `export_bundle_file`
- `provenance_bundle_file`

`solve` in user-request mode must return at least:

- the produced artifact,
- the runtime hash,
- whether the result is verified or best-effort,
- what checks ran,
- trace and artifact references if requested,
- and budget usage.

### 3.3 CLI ergonomics requirements

The CLI is the UI. Therefore:

- every command must work without opening Python code,
- every long-running build must leave behind inspectable artifacts,
- fatal errors must identify the failed stage and the frozen artifact involved,
- and exported runtimes must be runnable from the CLI without re-running evolution.

---

## 4. Layer-to-Layer Frozen Artifacts

A major defect in loose agentic systems is that later stages keep re-reading raw user text and silently doing fresh interpretation. Agintor must not do that. The system must write and then consume the following canonical artifacts.

1. **Raw goal input** → `GoalSpec`
2. `GoalSpec` → `SuccessCriteriaBundle`
3. `GoalSpec` + `SuccessCriteriaBundle` → `BenchmarkPlan`
4. `BenchmarkPlan` → `VerifierBundle`
5. `GoalSpec` + `BenchmarkPlan` + `VerifierBundle` → `RuntimePlan`
6. `RuntimePlan` → seed runtime artifact
7. seed runtime artifact → candidate runtime artifacts
8. candidate runtime artifact + frozen benchmark and verifier bundle → `SuiteEvaluation`
9. `SuiteEvaluation` stream → archive and validation records
10. selected leader → exported runtime artifact
11. exported runtime artifact + `SolveRequest` → `SolveResult`

Each artifact must be:

- serializable,
- written to disk in the build workspace,
- reloadable without hidden ambient state,
- and rich enough that the next stage does not need to reopen upstream free text except for provenance display.

---

## 5. Mandatory Schemas and Data Contracts

The implementation may use Pydantic models, dataclasses, or an equivalent typed representation. It may add fields. It may not omit the required fields listed below.

### 5.1 Build-time schemas

| Object | Required fields |
|---|---|
| `GoalSpec` | `goal_id`, `raw_prompt`, `normalized_goal`, `goal_keywords`, `goal_phrases`, `required_capabilities`, `constraints`, `success_criteria`, `target_families`, `deployment_preferences`, `assumptions`. |
| `SuccessCriterion` | `criterion_id`, `description`, `required`, `priority`, `measurable_signal`, `verifier_hint`, `target_family`, `weight`. |
| `BenchmarkPlan` | `plan_id`, `goal_id`, `family_targets`, `train_task_ids`, `proxy_task_ids`, `val_task_ids`, `test_task_ids`, `synthetic_task_ids`, `verifier_bundle_id`, `frozen`. |
| `VerifierSpec` | `verifier_id`, `verifier_type`, `artifact_contract`, `tolerance`, `uses_trace`, `local_only`, `expected_signal`. |
| `VerifierBundle` | `bundle_id`, `plan_id`, `verifiers`, `checker_chain_defaults`, `frozen`, `created_from`. |
| `FactoryProfile` | `agintor_provider`, `evaluation`, `evolution`, `mutation`, `benchmark_generation`, `leader_selection`, `runtime_backend`. |
| `RuntimePlan` | `plan_id`, `goal_id`, `runtime_abi`, `seed_template`, `mutable_files`, `immutable_manifest`, `runtime_profile`, `provider_plan`, `tooling_scope`, `deployment_contract`. |
| `DeploymentContract` | `entry_command`, `runtime_abi`, `python_version`, `supported_backends`, `required_env_names`, `network_policy`, `filesystem_policy`, `notes`. |
| `BuildSummary` | `build_id`, `goal_id`, `goal_spec_path`, `benchmark_plan_path`, `verifier_bundle_path`, `runtime_plan_path`, `workspace`, `output_runtime_dir`, `best_goal_score`, `best_val_score`, `accepted_mutations`, `archive_cells`. |

### 5.2 Runtime-time schemas

The runtime-time objects already present in the repository remain central and should be preserved.

| Object | Required fields |
|---|---|
| `AgentTemplate` | `agent_id`, `description`, `capability_set`, `symbol_set`, `default_tool_scope`, `success_stats`, `staleness_clock`, `model_policy_tag`. |
| `ChildSpec` | `child_id`, `role`, `instruction`, `tool_scope`, `model_class`, `required_capabilities`, `required_permissions`, `dependency_ids`, `comm_mode`, `resume_policy`, `init_summary`. |
| `ToolSpec` | `name`, `category_path`, `signature`, `description`, `runtime`, `deps`, `permissions`, `tests`, `backgroundable`, `state_schema`, `source_digest`, `build_cmd`, `run_cmd`, `timeout_s`, `determinism_class`. |
| `SummaryRecord` | `objective`, `evidence`, `artifacts`, `unresolved`, `open_handles`, `next_actions`, `symbols`, `verifier_state`, `provenance`. |
| `Checkpoint` | `summary`, `artifact_refs`, `open_handles`, `unresolved_goals`, `budget_state`, `verifier_state`, `resume_constraints`. |
| `AsyncHandle` | `handle_id`, `tool_name`, `sandbox_hash`, `working_directory`, `launch_time`, `timeout`, `stdout_path`, `stderr_path`, `state`, `artifact_refs`. |
| `MemoryNode` | `node_id`, `type`, `label`, `content`, `embedding`, `symbol_set`, `file_paths`, `source_task_id`, `verifier_support`, `timestamps`, `provenance`, `tombstoned`. |
| `BenchmarkTask` | `task_id`, `family`, `prompt`, `task_type`, `operations`, `expected`, `verifier_type`, `verification_required`, `allow_best_effort`, `transfer_scored`, `proxy_scope_tags`, `metadata`. |
| `SolveRequest` | `request_id`, `prompt`, `context_items`, `file_paths`, `output_schema`, `allowed_tool_categories`, `verification_preference`, `budget_overrides`. |
| `SolveResult` | `request_id`, `runtime_hash`, `artifact`, `status`, `summary`, `checks`, `trace_ref`, `budget`, `faults`. |
| `RuntimeManifest` | `runtime_id`, `version`, `policy_modules`, `mutable_files`, `immutable_manifest`, `metadata`. |
| `ArchiveEntry` | `code_hash`, `runtime_hash`, `scores`, `behavior_bin`, `scope_tag`, `complexity_bucket`, `mutable_loc`, `trace_refs`. |

### 5.3 Logical separation between factory profile and runtime profile

This is a necessary correction.

The current repository stores execution, evaluation, and evolution settings in one runtime profile file. The MVP may continue to use one physical file if necessary, but it must preserve the following **logical separation**:

- **Factory-only profile fields**: evaluation stage thresholds, crossover probability, mutator prompt IDs, archive selection parameters, validation seed counts, benchmark synthesis settings.
- **Runtime-only profile fields**: execution budgets, topology thresholds, memory thresholds, tooling thresholds, control thresholds, runtime provider mapping.
- **Shared declarative facts**: runtime ABI, model class names, backend compatibility, safety policy references.

The exported runtime artifact must not conceptually own factory-only knobs. If one JSON file is retained, it must be namespaced or reconstructible into separate `FactoryProfile` and `RuntimeProfile` objects.

---

## 6. Goal Interpretation and Success-Criteria Extraction

This stage belongs to the factory control plane. It occurs before benchmark planning and before seed runtime materialization.

### 6.1 Requirements

The system must take a natural-language goal and produce a `GoalSpec` that captures at least:

- normalized goal text,
- target capability set,
- hard and soft constraints,
- deployment preferences,
- target families,
- required success criteria,
- and explicit assumptions used to resolve ambiguity.

The build path must not halt just because the user did not provide a perfectly structured specification. Missing details should be resolved conservatively and recorded in `assumptions`.

### 6.2 Capability extraction

Capability extraction must identify what the produced runtime is expected to be good at. Examples include:

- decomposition and orchestration,
- exact memory retrieval,
- long-context management,
- dynamic tool reuse or synthesis,
- external artifact generation,
- verification-heavy solving,
- cost-sensitive solving,
- latency-sensitive solving,
- resumable or checkpointed workflows.

Capability extraction may use heuristics, model assistance, or both, but the result must be frozen into `GoalSpec`.

### 6.3 Constraint extraction

Constraint extraction must capture at least:

- allowed providers or provider classes,
- expected runtime backend (`local` or `docker`),
- filesystem and network assumptions,
- determinism preference,
- cost and latency sensitivity,
- preferred solve artifact type,
- whether best-effort output is acceptable,
- and whether the runtime is expected to export or persist reusable tools.

### 6.4 Success-criteria extraction

Every build must produce explicit success criteria. Each criterion must be mapped to:

- a measurable signal,
- a target benchmark family or cross-family pressure,
- and a verifier hint.

At minimum, success criteria must address:

1. correctness,
2. end-to-end completeness,
3. runtime efficiency,
4. robustness or determinism,
5. and at least one goal-specific property such as memory fidelity, tool reuse, or orchestration quality.

### 6.5 Family mapping

Benchmark families for the MVP remain:

- `top`
- `mem`
- `tool`
- `e2e`

This is deliberate. The mutable interfaces are `top`, `mem`, `tool`, and `ctl`, but **control is cross-cutting** and should not be made a peer benchmark family by default. In the MVP, control pressure is expressed through:

- proxy tasks,
- verification ladder behavior,
- stopping behavior,
- cost and latency penalties,
- and robustness metrics.

This keeps benchmark family taxonomy aligned with externally observable task behavior while still pressuring the control surface.

---

## 7. Benchmark Planning and Verifier Freeze

This stage translates the normalized goal into measurable evaluation pressure.

### 7.1 Benchmark planning requirements

The benchmark plan must:

- cover every target family in `GoalSpec`,
- include proxy, train, validation, and test partitions,
- preserve strict partition isolation,
- and freeze verifier logic before candidate evolution begins.

The MVP may start from the existing demo benchmark library and extend it through goal-conditioned cloning and bounded synthetic task generation.

### 7.2 Goal-conditioned benchmark pressure synthesis

The factory must support both:

1. **selection** from existing benchmark families and tasks, and
2. **bounded synthesis** of goal-conditioned tasks when the existing suite lacks sufficient pressure.

The synthesis strategy for the MVP should stay bounded and local. It must prefer tasks whose correctness can be judged by deterministic local verifiers. It may use provider assistance to propose task wording, expected artifact structure, or operation hints, but the final task and verifier must be executable locally and frozen before evaluation.

A valid benchmark plan should typically:

- select representative tasks from the demo or loaded suite per target family,
- clone or adapt those tasks with goal-conditioned emphasis,
- optionally create additional synthetic tasks for uncovered goal criteria,
- attach proxy tasks to specific mutable interfaces,
- and write all chosen task IDs and synthetic task IDs into `BenchmarkPlan`.

### 7.3 Verifier freeze

A critical invariant:

> candidate runtimes may decide **when** to ask for checks, but they may not redefine **what counts as correct** after the verifier bundle is frozen.

The verifier bundle must include at least:

- exact benchmark verifier specifications,
- local checker ladder defaults,
- trace-based proxy verifier definitions where needed,
- and any tolerance or artifact-shape contracts.

Validation and test verifier outputs must never be exposed to the mutator as improvement hints.

### 7.4 Supported verifier classes for the MVP

At minimum, the verifier system must support deterministic local variants of:

- exact JSON equality,
- numeric-tolerant JSON equality,
- exact string equality,
- exact numeric equality,
- trace event presence,
- trace event counts,
- artifact shape compatibility,
- and local checker ladders such as `local`, `subtree`, `repo`, and `benchmark`.

### 7.5 Benchmark tasks versus user solve requests

The system must keep a clean distinction between:

- **benchmark tasks**, which include an expected output or verifier contract used by the factory for evolution,
- and **user solve requests**, which may not include a benchmark oracle and must therefore run as verified-if-possible or best-effort solves.

Benchmark planning belongs to the factory. User solve adaptation belongs to the runtime host.

---

## 8. Runtime Planning and Bounded Artifact Definition

This stage resolves what the produced runtime actually is.

### 8.1 Runtime plan requirements

The `RuntimePlan` must resolve:

- runtime ABI version,
- seed template,
- mutable files,
- immutable manifest,
- runtime execution profile,
- runtime provider plan,
- allowed tooling scope,
- export path expectations,
- and deployment contract.

The runtime plan is frozen before evolution begins. The outer loop evolves candidate runtimes inside that plan. It does not keep redesigning the runtime’s identity mid-build.

### 8.2 What the produced runtime is

The produced runtime is:

- a manifest plus mutable policy code,
- executed by the fixed runtime host,
- under a frozen runtime ABI,
- with a runtime profile and export/provenance bundles,
- plus any promoted tool metadata or generated assets that the runtime needs at solve time.

The produced runtime is **not** the factory archive, the build history, or the benchmark plan.

### 8.3 Export artifact structure

The exported runtime directory must contain, at minimum:

```text
runtime_manifest.json
runtime_profile.json
runtime_export_bundle.json
runtime_provenance_bundle.json
deployment_contract.json
topology_policy.py
memory_policy.py
tool_policy.py
control_policy.py
```

Optional additional exported assets may include:

- promoted tool metadata,
- serialized generated tool specs,
- runtime-local examples,
- or additional immutable asset fingerprints.

The exported runtime may rely on an installed Agintor runtime host with a matching ABI. That is acceptable for the MVP. However, the export must still be self-describing and runnable without invoking the factory’s evolution pipeline.

### 8.4 Seed runtime materialization

The seed runtime must come from a concrete template, not from vague empty scaffolding. The existing baseline runtime template is the correct starting point. The factory may adjust the runtime profile for goal alignment, but the initial runtime must be loadable, executable, and benchmarkable before any mutation occurs.

---

## 9. Fixed Runtime Host and Immutable Shell Substrate

The fixed runtime host is the runtime-side execution kernel. It is shared across candidate runtimes and remains outside the mutable search surface.

### 9.1 Mandatory shell responsibilities

The runtime host and shell must own:

- runtime loading and ABI validation,
- canonical agent pool and clone-on-run semantics,
- short-term and long-term memory containers,
- message board state,
- open-handle table,
- tool registry and executor,
- safety validation and sandbox management,
- trace writing,
- solve request adaptation,
- and invariant validation.

### 9.2 Solve request adaptation belongs to the runtime host

This is an important placement rule.

The adapter from a user-facing `SolveRequest` into the runtime’s internal task envelope must belong to the fixed runtime host, not to the factory and not to mutable policies. The produced runtime must have a stable entrypoint for real user requests after export. Candidate runtimes may choose **how to solve** the adapted task, but not redefine the shape of the request contract itself on every mutation.

### 9.3 Graph contracts and invariants

The current graph contracts remain correct and must be preserved.

**Mandatory short-term node types:**

- `AgentRun`
- `Event`
- `Summary`
- `Artifact`
- `RawBlob`
- `OpenHandle`
- `VerifierEvidence`

**Mandatory short-term edges:**

- `CALLS_AGENT`
- `EMITS`
- `SUMMARIZES`
- `PRODUCES`
- `BACKLINKS_TO`
- `WAITS_ON`
- `CONTINUES_FROM`
- `VALIDATED_BY`

**Mandatory long-term node types:**

- `Symbol`
- `File`
- `Query`
- `Answer`
- `ToolFailure`
- `FixPattern`
- `TaskNote`
- `Procedure`
- `EnvironmentFingerprint`
- `ArtifactSignature`

### 9.4 Non-negotiable shell invariants

The shell must enforce at least the following invariants:

1. The canonical stored agent is never executed directly; every invocation uses a clone.
2. Horizontal workers do not share mutable solve state; they share only the append-only message board and deterministic merge inputs.
3. Message-board state and open-handle tables survive compaction and resume.
4. Short-term compaction may hide raw nodes, but raw-output reachability through backlinks must be preserved.
5. Long-term memory resets between independent tasks unless transfer is explicitly being scored.
6. Category-first tool discovery is mandatory; full-registry prompt stuffing is forbidden.
7. Sandbox reuse must be content-addressed.
8. Exact symbol and path matches dominate embedding similarity in retrieval and deduplication.
9. Merge order is deterministic.
10. Validation and test traces may never enter mutation prompts.

### 9.5 Safety ownership

Mutable policies may propose tools and dispatch paths, but the shell owns:

- validation of tool safety,
- sandbox environment creation,
- forbidden import and call checks,
- and execution isolation.

The runtime must not be able to bypass these through mutable policy code.

---

## 10. Mutable Runtime Policy Surface

The search surface must stay narrow and meaningful. The mutable runtime policies control behavior at solve time. They do not own factory planning, archive logic, or verifier definitions.

### 10.1 Topology policy

**Responsibilities:**

- choosing single, vertical, or horizontal execution mode,
- scoring agent reuse versus creation,
- proposing children,
- selecting workers,
- assigning tool scope to children,
- deterministic ensemble merge,
- checkpoint creation.

**Mandatory mutable methods:**

- `score_agent`
- `select_mode`
- `propose_children`
- `select_workers`
- `assign_scope`
- `merge_ensemble`
- `make_checkpoint`

**Must not own:**

- archive state,
- benchmark generation,
- direct sandbox execution,
- or leader selection.

### 10.2 Memory policy

**Responsibilities:**

- short-term compaction span selection,
- summarization of selected spans,
- long-term retrieval ranking,
- memory promotion scoring,
- promotion decision,
- deduplication action selection,
- and long-term upsert behavior.

**Mandatory mutable methods:**

- `select_spans_for_compaction`
- `summarize_span`
- `retrieve_long_term`
- `score_memory_unit`
- `should_promote`
- `dedup_candidates`
- `upsert_memory`

**Must not own:**

- shell graph integrity rules,
- memory reset policy across evaluation units,
- or raw-node reachability invariants.

### 10.3 Tooling policy

**Responsibilities:**

- category ranking,
- tool ranking,
- build-vs-reuse decision,
- proposed tool specification,
- tool validation opinion,
- promotion decision,
- dispatch metadata.

**Mandatory mutable methods:**

- `rank_categories`
- `rank_tools`
- `should_create_tool`
- `propose_tool_spec`
- `validate_tool`
- `promote_tool`
- `dispatch_tool`

**Must not own:**

- safety bypass,
- environment creation,
- secret access,
- or unchecked subprocess execution.

### 10.4 Control policy

**Responsibilities:**

- model assignment,
- checker request selection,
- stopping policy.

**Mandatory mutable methods:**

- `assign_model`
- `request_checks`
- `stop_policy`

**Factory-side responsibilities that must not be placed inside exported runtime control policy:**

- archive objective selection,
- scope scheduler state,
- phase advancement,
- archive cell keys,
- leader selection,
- counterfactual credit updates.

Solve-time control is mutable; factory-side evolutionary accounting is not.

### 10.5 Why outer-loop scope credit does not belong to runtime control

Archive scope credit and scheduler updates do not belong inside the runtime control surface. For the MVP, that separation must be explicit:

- candidate runtimes may emit solve-time telemetry or observable behavior,
- but only the factory control plane updates scope credit, phase progression, or archive insertion logic.

The runtime may influence those outcomes only indirectly through its measured behavior.

---

## 11. Task-Time Runtime Execution State and Orchestration

The runtime host executes tasks through a state machine induced by the loaded runtime.

### 11.1 Required runtime state

The task-time state must include, at minimum:

- active frame queue,
- visible tool names,
- unresolved goals,
- current confidence estimate,
- selected mode,
- artifact map,
- checkpoints,
- worker plans,
- open handle IDs,
- created tool count,
- promoted node count,
- checks used,
- subgoal negative-step counters,
- subgoal last-model map,
- budget state,
- and trace rows.

### 11.2 Root and child execution

The runtime must support:

- a root frame,
- vertically spawned child frames with checkpoints,
- horizontally isolated workers,
- deterministic merge of worker outputs,
- and resume-aware checkpoint publication.

### 11.3 Solve modes

The runtime must support at least:

- `single`
- `vertical`
- `horizontal`

The topology policy decides which to use. The runtime host executes the choice.

### 11.4 User-request mode versus benchmark mode

The runtime host must support two entry modes:

1. **benchmark mode**, where a `BenchmarkTask` is executed under a known verifier,
2. **user-request mode**, where a `SolveRequest` is converted into a bounded internal task envelope.

For user-request mode, the MVP may stay bounded by restricting the adapted internal task representation to the supported operation and tool model. If no exact verifier exists, the runtime still runs but must report whether the result is verified or best-effort.

### 11.5 Memory ingestion and compaction

Context items provided by a benchmark task or user solve request must be ingested into short-term memory and, when appropriate, promoted into long-term memory through the mutable memory policy and the fixed long-term graph interface.

Compaction is triggered by runtime budget pressure and must preserve raw-output reachability. Compaction belongs to solve time. The high-water and low-water thresholds belong to the runtime profile.

### 11.6 Tool discovery and execution

Tool use must follow this pipeline:

1. category ranking,
2. category slice selection,
3. candidate tool collection,
4. tool ranking,
5. build-vs-reuse decision,
6. optional tool synthesis proposal,
7. safety validation,
8. dispatch,
9. optional async handle tracking,
10. promotion decision.

The runtime must not jump directly from operation description to unrestricted arbitrary tool generation.

### 11.7 Verification ladder and stopping

The control policy may choose which checkers to request, but the available checker ladder and benchmark verifier definitions are frozen by the verifier bundle.

The stop policy may terminate only under budget exhaustion or when the runtime believes further action has negative utility, but if the task requires verification and a verified terminal artifact is still required, the runtime must not terminate as if the task were complete.

---

## 12. Evaluation, Scoring, Robustness, and Archive Insertion

All of this belongs to the factory control plane.

### 12.1 Evaluation unit

An evaluation unit is either:

- a single task, or
- an ordered multi-task episode when transfer itself is being scored.

Dynamic agents, dynamic tools, and short-term memory reset between tasks. Long-term memory resets between independent tasks unless transfer is explicitly part of the benchmark objective.

### 12.2 Staged evaluation gates

The staged evaluator in the current repository is the correct core pattern and should be preserved and completed.

**Stage 0: patch and contract integrity**

- patch format is valid,
- patch size is within limits,
- changed lines stay inside contracted mutable methods,
- resulting runtime parses and loads.

**Stage 1: deterministic smoke**

- repeated execution on a smoke proxy task yields identical artifacts, verifier scores, modes, and normalized traces.

**Stage 2: proxy gate**

- compare parent and child on proxy tasks aligned with the touched interfaces,
- reject clear regressions or invalid runs.

**Stage 3: local subset gate**

- compare parent and child on a subset centered on the selected objective,
- reject clear regressions or invalid runs.

**Stage 4: full train gate**

- evaluate across the full train plan,
- allow minibatch early rejection for clear global regression,
- score only after the full stage succeeds.

Validation and test are used after archive insertion for leader tracking and final export selection, not for mutator guidance.

### 12.3 Utility and robustness

The scoring model from the current repository is appropriate and should remain the baseline.

For one run:

\[
u = V - \lambda_C \log(1 + C / C_0) - \lambda_L \log(1 + L / L_0) - \lambda_H H
\]

where:

- \(V\) is the benchmark verifier score,
- \(C\) is cost,
- \(L\) is latency,
- \(H\) is operational faults,
- \(C_0\) and \(L_0\) are task-level reference scales.

Task-level mean utility is:

\[
s = \frac{1}{R}\sum_{r=1}^{R} u_r
\]

Robustness-adjusted utility is:

\[
\rho = s - \kappa_b \hat{\sigma} - \kappa_u \hat{\sigma}/\sqrt{R}
\]

with shrinkage variance estimate \(\hat{\sigma}\).

The lower-tail CVaR statistic should remain a validation and leader tie-break signal.

### 12.4 Objective families

Objective names should continue to include:

- single-task objectives, `s:<task_id>`
- family means, `sbar:<family>`
- family robustness means, `rhobar:<family>`
- global mean, `sbar:global`
- global robustness mean, `rhobar:global`

### 12.5 Archive design

The archive must preserve diversity over at least:

- objective,
- interface difference mask,
- behavior descriptor,
- scope tag,
- and complexity bucket.

The current direction is correct:

- behavior bins summarize dominant mode, created-tool rate, promotion density, and check density,
- complexity buckets reflect mutable size or AST-node volume,
- scope tags preserve which interfaces changed.

### 12.6 Parent selection, scope scheduling, and crossover

These belong to the factory control plane.

The factory must own:

- objective selection,
- scope sampling,
- parent selection,
- counterfactual contribution analysis,
- phase advancement from local to pair to joint scopes,
- and crossover application.

The runtime control policy must not update scheduler state directly.

### 12.7 Training, validation, and test isolation

A hard invariant:

- train traces may inform mutation,
- validation traces may inform only factory-side leader choice and phase advancement,
- test traces are final measurement only,
- and neither validation nor test traces may enter mutation prompts or mutation heuristics.

---

## 13. Provider Backends and Isolation

### 13.1 Two provider roles

The MVP must clearly distinguish:

- the **Agintor provider**, used by the factory for mutation prompts, benchmark synthesis assistance, or verifier-adaptation assistance,
- and the **runtime provider**, used by the exported runtime during task execution.

These may be the same provider implementation or different ones. Their roles must remain distinct.

### 13.2 Provider ownership rules

The provider layer owns:

- API request execution,
- response accounting,
- environment variable resolution,
- API key file loading,
- replay and local deterministic providers,
- and container payload serialization.

The provider layer must not own:

- benchmark semantics,
- archive semantics,
- runtime identity hashes,
- or secret persistence inside export bundles.

### 13.3 Export secret handling

The exported runtime may store provider names, model maps, and required environment variable names. It must not store live API keys. Secrets remain external.

### 13.4 Runtime backend support

The MVP must support at least:

- `local` runtime execution, and
- `docker` runtime execution.

The existing container runtime pattern is valid: the factory or runtime host may package runtime execution inside a container while mounting the exported runtime, task payloads, and provider payloads.

---

## 14. Workspace Layout, Reports, and Export Contract

The build workspace must make the pipeline inspectable.

### 14.1 Minimum workspace outputs

A successful `build-runtime` execution must write, at minimum:

```text
workspace/
  goal/
    goal_spec.json
    success_criteria.json
  planning/
    benchmark_plan.json
    verifier_bundle.json
    runtime_plan.json
    factory_profile.json
  seed_runtime/
    ...
  evolution/
    evolution_history.json
    archive_index.json
    validation_history.json
    stage_failures.json
  export/
    build_summary.json
    leaderboard.json
    export_summary.json
```

Exact subdirectory names may vary, but the logical outputs must exist.

### 14.2 Exported runtime bundles

The exported runtime must include:

- an export bundle with runtime hash, code hash, ABI, runtime ID, provider identity, and source runtime information,
- a provenance bundle with artifact digests and attestation hash,
- a deployment contract that tells the user how to run the runtime,
- and the runtime manifest plus mutable policy files.

### 14.3 Runtime identity and portability

The runtime identity must depend on:

- manifest content,
- mutable policy file digests,
- immutable manifest digests,
- and the effective runtime profile.

It must not depend on transient bytecode files, timestamps, or hidden process state.

### 14.4 Inspectability

The user must be able to inspect:

- what goal the build normalized to,
- which benchmark families were selected,
- which verifiers were frozen,
- which runtime profile was resolved,
- what leader was exported,
- and what the exported runtime requires to run.

---

## 15. Recommended Defaults

These defaults are appropriate for the MVP and consistent with the current repository direction.

| Parameter group | Recommended defaults |
|---|---|
| Runtime interfaces | mutable surfaces are `top`, `mem`, `tool`, `ctl`; benchmark families are `top`, `mem`, `tool`, `e2e` |
| Evaluation seeds | proxy: `1`; subset: `1`; full train: `3`; validation: `5`; held-out final: `5` or `7` |
| Stage gates | deterministic smoke must pass repeated local replays; proxy and subset gates use LCB-style regression rejection |
| Runtime budgets | `max_steps=64`, `model_calls_max=64`, `checks_max=16`, `context_window_tokens≈768` |
| Topology defaults | `theta_create≈0.58`, `k_max≈3` |
| Memory defaults | `b_hi≈0.75`, `b_lo≈0.55`, promotion and dedup thresholds near the current baseline values |
| Tool defaults | category slice `k_c≈3`, promotion requires safety plus repeated successful reuse |
| Evolution budgets | local scopes: about `1200`; pair scopes: about `600`; joint scopes: about `300` |
| Crossover | enabled but low probability, around `0.15` |
| Robustness | shrinkage variance with `eta_sigma≈0.35`; use robustness-adjusted score for search and CVaR-like tail risk for tie-breaks |
| Providers | local deterministic provider required; hosted providers optional; exported runtime stores provider contract, not secrets |

If a hosted endpoint cannot provide strong determinism, set temperature to zero and keep repeated-seed evaluation enabled.

---

## 16. Acceptance Criteria

A build is only complete if the system demonstrates the following as actual behavior, not just as source code.

### 16.1 CLI and product behavior

1. `agintor build-runtime "<goal>" --destination <dir>` completes the full build pipeline and exports a runtime.
2. The build writes goal, benchmark, verifier, runtime-plan, and export artifacts into the workspace.
3. The build does not require the user to manually write benchmark suites for the golden path.
4. The build summary identifies the exported runtime and its leader metrics.

### 16.2 Runtime separation

5. The exported runtime can be loaded and run without entering the evolution path.
6. The produced runtime artifact contains only runtime-relevant assets and not the full factory archive or mutator history.
7. Factory-only profile knobs are logically separated from runtime-only execution knobs.

### 16.3 Solve path

8. `agintor solve <runtime_dir> --task-id ... --suite ...` works in benchmark mode.
9. `agintor solve <runtime_dir> --prompt ...` or the equivalent user-request mode works on a real solve request after export.
10. The solve result reports whether the output is verified or best-effort.

### 16.4 Evolution and evaluation

11. Stage 0 through Stage 4 evaluation gates function and reject invalid or regressive candidates.
12. Archive insertion, parent selection, and validation leader tracking function end-to-end.
13. Goal-conditioned tasks or benchmark-pressure synthesis are actually used during `build-runtime`, not merely described in comments.

### 16.5 Core runtime behavior

14. Topology mode selection, memory retrieval, tool reuse or synthesis, and control checks are all exercised by the shipped demo suite.
15. Dynamic tool creation is validated locally before promotion.
16. Memory compaction preserves raw-output reachability.
17. Clone-on-run invariants are enforced.

### 16.6 Provenance and deployability

18. The export bundle and provenance bundle are written for the leader runtime.
19. The deployment contract identifies the required runtime host, backend compatibility, and provider environment expectations.
20. Provider secrets are not embedded in the exported runtime.

---

## 17. Implementation Order

A correct reconstruction order for the coding agent is:

1. preserve and complete the runtime host and shell invariants,
2. preserve and complete the baseline runtime template,
3. define or complete the build-time schemas and workspace artifacts,
4. implement goal normalization and success-criteria extraction,
5. implement benchmark planning and verifier freezing,
6. resolve the split between factory profile and runtime profile,
7. complete the CLI golden path around `build-runtime`,
8. complete the user-request solve path for exported runtimes,
9. finalize staged evaluation, archive insertion, and validation leader tracking,
10. finalize export and provenance bundles,
11. then tighten defaults and deterministic behavior.

Do not start by redesigning the entire project into a different architecture. The existing repository already contains the correct core spine.

---

## 18. Failure Modes and Non-Negotiable Invariants

The following failures materially change the method and are therefore not allowed.

1. Mutating the immutable shell or benchmark/verifier bundle during candidate evolution.
2. Executing canonical stored agents directly instead of clone-on-run.
3. Allowing long-term memory leakage across nominally independent tasks.
4. Promoting synthesized tools without local validation, safety checks, and reuse evidence.
5. Destroying raw-output reachability during compaction.
6. Losing message-board or open-handle state during summarization or resume.
7. Using validation or test traces as mutation guidance.
8. Letting later pipeline stages reparse raw goal text instead of consuming frozen planning artifacts.
9. Storing provider secrets in exported runtime artifacts.
10. Allowing the exported runtime to mutate archive state or factory scheduler state.
11. Bypassing category-first discovery by loading the entire tool registry into prompt context.
12. Terminating without a verified terminal artifact when the task contract requires verification.
13. Exporting a runtime without an ABI, provenance, and deployment contract.
14. Treating the produced runtime artifact and the factory control plane as the same thing.

---

## 19. Closing Statement

Agintor is not a prompt-tuning script and not merely a benchmark runner. It is a bounded runtime factory for evolving multi-agent runtimes under frozen benchmark pressure and explicit architectural boundaries.

The MVP is complete only when the repository produces all of the following as real CLI behavior:

- a normalized goal artifact,
- a frozen benchmark and verifier plan,
- a resolved runtime plan,
- a seeded and evolved runtime,
- a validated leader,
- an exported runtime artifact,
- and a user-facing solve path for that exported runtime.

Anything less is still a partial scaffold.
