# Trace Correlation, Scoped OpenAI Logs, Wire-Faithful Rendering, and Goal-Conditioned Benchmark Planning

## Summary

Implement a typed trace-correlation contract, generate scoped OpenAI trace views by build and runtime task, make the human-readable transcript render only true API traffic, and strengthen goal-conditioned benchmark planning so rich goals produce broader and more meaningful evaluation pressure.

This work updates the factory, host/runtime request contracts, runtime-side provider call sites, trace storage and rendering, and benchmark-planning logic. The runtime ABI and storage schema should be bumped once at the start of the work because the host/runtime request shape and persisted trace format will change.

## Implementation Changes

### 1. Add typed correlation context to every OpenAI call

- Add a typed `OpenAITraceContext` schema under runtime or provider-facing contracts with these optional fields:
  - `session_id`
  - `provider_role`
  - `build_id`
  - `runtime_hash`
  - `runtime_dir`
  - `task_id`
  - `seed`
  - `request_id`
  - `iteration`
  - `objective`
  - `touched_scope`
  - `agent_id`
  - `frame_role`
  - `worker_id`
  - `op_id`
  - `run_node_id`
- Keep `metadata.mode` for purpose classification only. Move all cross-cutting identity and correlation data into `metadata.trace_context`.
- Update factory-side call sites:
  - `runtime_builder.py` planning refinement calls must include build-level context.
  - `mutator.py` provider mutation and repair calls must include build, iteration, objective, and touched-scope context.
  - `evolution.py` should propagate iteration and candidate context into mutation requests.
- Update runtime-side call sites:
  - `TaskRuntime` and `PolicyContext` should carry request, task, seed, and runtime identity.
  - `memory_policy.py` summary requests should include task, seed, frame, and run-node context.
  - `tool_policy.py` tool-spec requests should include task, seed, frame, op, and run-node context.
  - `runner.py` direct user-response requests should include request, task, frame, and op context.
- Add helper builders so trace context is assembled in one place instead of ad hoc dictionaries.
- Update `persist_openai_trace` and stored JSON records to persist `trace_context` as a first-class field separate from `request_metadata` and `request_payload`.

### 2. Finalize traces into scoped views by build and runtime task

- Keep one canonical raw call store as the source of truth.
- Build scoped rendered views from canonical call records instead of flattening everything into one transcript.
- Update trace storage layout to support:

```text
openai_api_traces/
  sessions/<session_id>/
    calls/*.json
    calls/*.md
    INDEX.md
    TRANSCRIPT.md
    builds/<build_id>/
      INDEX.md
      TRANSCRIPT.md
      runtime_tasks/<task_id>/seed_<seed>/
        INDEX.md
        TRANSCRIPT.md
    solves/<request_id>/
      INDEX.md
      TRANSCRIPT.md
```

- Do not duplicate raw call JSON into scoped folders. Scoped folders should contain rendered indexes and transcripts only.
- Add a trace finalization pass that:
  - groups calls by `build_id`
  - groups runtime calls by `task_id` and `seed`
  - groups solve calls by `request_id`
  - writes scoped indexes and transcripts after `build-runtime`, `evolve`, `eval`, and `solve`
- Keep the existing `agintor.openai_trace` rendering utility, but extend it to build grouped outputs from canonical records instead of only date-based subsets.
- Ensure interrupted or failed runs can be re-finalized later from canonical call files without rerunning the build or solve.

### 3. Make the human-readable transcript wire-faithful

- Redefine the readable trace so `Outgoing` contains only the actual OpenAI request content:
  - `instructions`
  - `input`
  - useful request envelope fields such as `model`, `reasoning`, and `max_output_tokens`
- Redefine `Incoming` so it contains only the actual model response:
  - parsed structured content from `response_raw` when present
  - fallback to `response_text`
- Remove local orchestration metadata from the visible API conversation body.
- Stop rendering `request_metadata.payload` as part of the human-facing request body for:
  - `planning`
  - `patch`
  - `patch_repair`
  - `tool_spec`
  - `summary`
  - `user_request`
- Add a separate `Call Context` section in rendered traces that shows:
  - `trace_context`
  - `purpose`
  - token counts
  - latency
  - provider role
- Keep formatting improvements already added:
  - pretty-printed JSON
  - wrapped prose
  - readable SEARCH/REPLACE blocks
- Add a strict render rule: visible `Outgoing` content must round-trip to recorded wire request fields without inventing additional request body content.
- Keep an optional verbose render mode for internal debugging that includes local metadata, but make wire-faithful mode the default.

### 4. Expand goal-conditioned benchmark planning

- Remove the `max_families=2` cap from goal-family selection in `goal_rubric.py`.
- Keep all families whose score indicates actual relevance. Since there are only four families, do not introduce another small hard cap.
- Replace one-task-per-family selection with multi-task-per-family selection in `runtime_builder.py`.
- Change train planning defaults to:
  - at least 2 train tasks per selected family when available
  - at least 1 proxy task per selected family
  - preserve cross-family `e2e` pressure whenever the goal implies orchestration, verification, export, or composite workflows
- Keep provider-assisted planning conservative. It may revise structured planning artifacts, but local planning must no longer compress a rich goal into only one representative task per family.
- Add a deterministic coverage pass before freezing `BenchmarkPlan`. It should compare:
  - required capabilities
  - success criteria
  - selected families
  - selected train tasks
  - selected proxy tasks
- Write a new `planning/benchmark_provenance.json` artifact containing:
  - family scoring
  - selected families
  - selected tasks by partition
  - coverage of required capabilities
  - uncovered criteria
  - clone provenance
  - synthetic-task provenance
- Replace the current strategy name with one that reflects actual behavior. Do not keep `goal_conditioned_demo_clone` once planning supports broader selection.
- If coverage remains weak after multi-task selection, generate bounded goal-conditioned tasks from typed local templates only. Do not allow free-form provider-authored benchmark creation.
- Implement bounded template families such as:
  - `top`: dependency-heavy decomposition and merge variants
  - `mem`: exact retrieval with compaction or resume pressure
  - `tool`: under-specified synthesis and reuse-vs-build variants
  - `e2e`: multi-operation composite tasks joining top, mem, and tool behaviors
- Every cloned or synthesized task must record:
  - `source_task_id`
  - `template_id`
  - `goal_criteria_targets`
  - `transform_summary`
  - `verifier_origin`

## Public Interfaces and Contract Changes

- Add `trace_context` to runtime-side request contracts and provider call metadata.
- Add `OpenAITraceContext` as a typed schema.
- Add `planning/benchmark_provenance.json` to the build workspace.
- Update the session trace schema to include `trace_context` as a stored field.
- Bump `runtime_abi` and `storage_schema_version` once to reflect the request-contract and persisted-trace changes.

## Validation

- Manual validation:
  - Run `build-runtime` with a hosted provider and confirm every OpenAI call record contains correct trace context.
  - Confirm trace outputs are grouped by build, runtime task, and solve request instead of one flat mixed transcript.
  - Confirm visible `Outgoing` request bodies contain only wire payload fields and no local metadata payloads.
  - Confirm a rich goal selects more than two relevant families when justified.
  - Confirm benchmark planning selects multiple train tasks per selected family and writes `benchmark_provenance.json`.
- Focused regression tests:
  - trace-context serialization and propagation
  - scoped trace finalization from canonical call records
  - wire-faithful transcript rendering
  - uncapped family selection
  - multi-task-per-family benchmark selection
  - bounded template-based goal-conditioned task generation provenance

## Assumptions

- No new end-user CLI command is required for this pass. Existing commands should finalize scoped traces automatically, and the trace module can still be used directly for rebuild or repair.
- Canonical per-call JSON remains the only raw source of truth.
- Missing correlation fields are omitted rather than synthesized.
- Benchmark planning remains bounded and locally judgeable. No open-ended grader or benchmark invention is introduced.

## Implementation Decisions

### Trace Context Contract

- Define `OpenAITraceContext` in `agintor/schemas.py` as the canonical typed contract.
- Carry `OpenAITraceContext` as a real field on runtime request schemas.
- Project `OpenAITraceContext` into `ModelRequest.metadata.trace_context` immediately before provider execution.
- Treat `ModelRequest.metadata.trace_context` as a transport projection, not the source of truth.
- Standardize `provider_role` to exactly:
  - `factory`
  - `runtime`
- In benchmark mode, set `request_id` to `benchmark.<task_id>.seed_<seed>`.
- For mutation calls, set `runtime_hash` and `runtime_dir` to the parent runtime being mutated.
- Assign `session_id` per top-level CLI invocation.

### Trace Grouping and Rendering

- Group runtime-task traces by:
  - `build_id`
  - `task_id`
  - `seed`
  - `runtime_hash`
- Use this layout for runtime-task views:

```text
builds/<build_id>/runtime_tasks/<task_id>/seed_<seed>/runtimes/<runtime_hash>/
```

- Keep `iteration` in trace headers and indexes, not as the primary folder key.
- When prompt-mode solve runs during export validation inside a build, include its calls in both:
  - the build-scoped trace view
  - `solves/<request_id>/`
- Render `Outgoing` from `request_payload` only.
- Use top-level stored `instructions` and `input` only as fallback for older records that do not contain a complete request payload.
- Render `Incoming` using this precedence:
  - read `response_raw.output` in order
  - take `message` items only
  - within each `message`, take `content` items in order
  - concatenate `output_text` item text when present
  - otherwise render refusal text when present
  - otherwise pretty-print the first structured message content object
  - fall back to `response_text` only if none of the above yields visible content
- Do not render `reasoning` items as the default incoming message body.
- Make `calls/*.md` wire-faithful by default, with the same rendering rules as grouped transcripts.

### Goal-Scoped Benchmark Planning

- Make `BenchmarkPlan` a goal-scoped subset rather than preserving the full demo train set by default.
- Keep the demo suite as the source library, then freeze only the selected tasks into the plan.
- Replace family selection with this exact rule:
  - compute all family scores
  - select every family with score `>= 2`
  - if none meet that threshold, select the top 2 positive-scoring families
  - if no family has a positive score, default to `["e2e", "top"]`
  - force-include `e2e` if the goal implies export, deployment, verification, orchestration, workflow completion, or composite reports
- Change train planning defaults to:
  - at least 2 train tasks per selected family when available
  - at least 1 proxy task per selected family when available
  - preserve cross-family `e2e` tasks when composite workflow pressure is implied
- Trigger bounded synthetic task generation if any of these remain true after deterministic selection:
  - a required capability is uncovered
  - a required success criterion is uncovered
  - a selected family has fewer than 2 train tasks when 2 or more are available
  - a selected family has no proxy task when a proxy exists for that family
- Allow provider-assisted planning to propose revised selected task IDs and approved template IDs only within the known benchmark library and approved local templates.
- Keep the local deterministic planner as the final owner of task selection, verifier selection, and final artifact freeze.

### Benchmark Provenance and Versioning

- Add `planning/benchmark_provenance.json` with this shape:

```json
{
  "artifact_metadata": {},
  "provenance_id": "benchmark-provenance.<id>",
  "goal_id": "<goal_id>",
  "benchmark_plan_id": "<plan_id>",
  "strategy": "goal_scoped_multi_select_v1",
  "selected_families": ["top", "mem", "tool", "e2e"],
  "family_scores": {
    "top": 0,
    "mem": 0,
    "tool": 0,
    "e2e": 0
  },
  "required_capabilities": [],
  "capability_coverage": {
    "covered": [],
    "uncovered": [],
    "coverage_by_task": {}
  },
  "success_criteria_coverage": {
    "covered": [],
    "uncovered": [],
    "coverage_by_task": {}
  },
  "partition_selection": {
    "train_task_ids": [],
    "proxy_task_ids": [],
    "val_task_ids": [],
    "test_task_ids": []
  },
  "source_tasks": [
    {
      "task_id": "",
      "family": "",
      "selection_kind": "selected",
      "source_task_id": null,
      "template_id": null,
      "transform_summary": null,
      "goal_criteria_targets": [],
      "verifier_origin": "copied"
    }
  ],
  "synthesis_decision": {
    "triggered": false,
    "reasons": [],
    "templates_used": []
  },
  "provider_assist": {
    "used": false,
    "authority": "propose_revisions_subject_to_local_validation",
    "applied_revisions": []
  }
}
```

- Use these initial bounded template IDs:
  - `top.multi_op_structured_v1`
  - `top.checkpoint_trace_variant_v1`
  - `mem.exact_symbol_compaction_v1`
  - `mem.exact_path_resume_v1`
  - `tool.underspecified_expression_v1`
  - `tool.reuse_vs_create_variant_v1`
  - `e2e.composite_numeric_report_v1`
  - `e2e.composite_memory_tool_v1`
- Restrict template transforms so they may:
  - change prompt wording
  - change literals, symbol names, row values, context volume, and dependency annotations
  - omit an explicit expression for under-specified generated-expression tasks
- Restrict template transforms so they may not:
  - change task family
  - change verifier class
  - change artifact shape or output keys
  - introduce external side effects
  - broaden allowed tool categories beyond the source task family
- Set `FactoryProfile.benchmark_generation.strategy` to `goal_scoped_multi_select_v1`.
- Set:
  - `runtime_abi = "agintor-runtime-abi-v5"`
  - `storage_schema_version = "agintor-storage-v3"`
- Treat compatibility failures as hard breaks with explicit loader or host errors that report expected versus actual values and instruct the user to rebuild or re-export the runtime.

## Went To Which Implementation Workstream

This plan was distributed across Workstreams 2 through 5. Workstream 1 is already implemented and remains the prerequisite boundary for this plan, but no new scope from this document should be implemented in Workstream 1.

Use this section as the routing map when implementing from the remaining workstreams. If a change is listed under another workstream, treat it as context only and do not re-implement it in your own file.

| Plan area | Primary implementation workstream | Secondary workstreams | Notes |
| --- | --- | --- | --- |
| Typed `OpenAITraceContext` contract | Workstream 2 | Workstreams 4, 5, 3 | Workstream 2 owns runtime-side request, execution-plan, frame, and policy-context contracts. Workstream 4 stamps factory-side planning and mutation calls. Workstream 5 projects the contract into provider metadata and raw call records. Workstream 3 stores and groups it durably. |
| `trace_context` added to runtime-side request contracts and provider call metadata | Workstream 2 | Workstream 5 | Workstream 2 owns request-side schemas and runtime-native propagation. Workstream 5 owns provider-facing metadata projection and preservation. |
| Factory-side planning refinement trace context | Workstream 4 | Workstream 5 | Workstream 4 owns `runtime_builder.py` planning calls and build-scoped context. Workstream 5 only needs to preserve the fields once they reach the provider layer. |
| Factory-side mutation and patch-repair trace context | Workstream 4 | Workstream 5 | Workstream 4 owns `mutator.py` and `evolution.py` context assembly for iteration, objective, touched scope, and parent runtime identity. |
| Runtime-side trace context on `TaskRuntime`, `PolicyContext`, frames, operations, and solve requests | Workstream 2 | Workstream 5 | Workstream 2 owns the runtime execution contract and propagation. Workstream 5 consumes that context when provider calls are made. |
| Canonical raw OpenAI call store | Workstream 3 | Workstream 5 | Workstream 3 owns the durable trace store and rebuildability. Workstream 5 owns what is captured into each per-call record. |
| Session-scoped trace layout under `openai_api_traces/` | Workstream 3 | Workstream 5 | Workstream 3 owns directory layout, grouping, re-finalization, and scoped transcript generation. Workstream 5 owns per-call record semantics and default call rendering. |
| Scoped trace finalization by build, runtime task, seed, runtime hash, and solve request | Workstream 3 | Workstream 2 | Workstream 3 owns grouped views and rebuild logic. Workstream 2 provides stable request and runtime identity fields needed for grouping. |
| Dual inclusion of export-validation prompt solves in build and solve trace views | Workstream 3 | Workstream 2 | Workstream 3 owns the grouped-finalization rule. Workstream 2 provides the normalized request and runtime context that makes this grouping unambiguous. |
| Wire-faithful `Outgoing` rendering | Workstream 5 | Workstream 3 | Workstream 5 owns the rule that visible outgoing content comes from wire payload fields only. Workstream 3 applies the same rule when rebuilding grouped transcripts from canonical records. |
| Wire-faithful `Incoming` rendering from preserved response envelopes | Workstream 5 | Workstream 3 | Workstream 5 owns response-envelope preservation and extraction rules. Workstream 3 reuses those rules when generating grouped transcripts later. |
| `calls/*.md` wire-faithful by default | Workstream 5 | Workstream 3 | Workstream 5 owns per-call rendering. Workstream 3 owns the grouped transcript rebuild surface that should match it. |
| Optional verbose trace render mode | Workstream 5 | Workstream 3 | Workstream 5 owns per-call verbose mode. Workstream 3 may expose rebuild or repair flows that use the same mode options, but does not redefine the semantics. |
| Remove `max_families=2` and adopt the explicit family-selection rule | Workstream 4 | None | Entirely benchmark-planning scope. |
| Make `BenchmarkPlan` a goal-scoped subset rather than the full demo train set by default | Workstream 4 | None | Entirely benchmark-planning and evaluation-prep scope. |
| Multi-task-per-family train and proxy selection defaults | Workstream 4 | None | Entirely benchmark-planning scope. |
| Deterministic benchmark coverage pass before freeze | Workstream 4 | None | Entirely benchmark-planning scope. |
| `planning/benchmark_provenance.json` artifact and schema | Workstream 4 | None | Entirely benchmark-planning scope. |
| Replace planning strategy name with `goal_scoped_multi_select_v1` | Workstream 4 | None | Entirely benchmark-planning scope. |
| Bounded goal-conditioned task synthesis from approved local templates | Workstream 4 | None | Entirely benchmark-planning and benchmark-library scope. |
| Initial bounded template IDs and transform restrictions | Workstream 4 | None | Entirely benchmark-planning scope. |
| Provider-assisted planning authority limits | Workstream 4 | Workstream 5 | Workstream 4 owns what provider-assisted planning is allowed to change. Workstream 5 only preserves structured provider outputs and trace capture for those calls. |
| `runtime_abi = agintor-runtime-abi-v5` and `storage_schema_version = agintor-storage-v3` | Workstream 2 | Workstreams 3, 5 | Workstream 2 owns introducing the version bump at the runtime contract boundary. Workstreams 3 and 5 must adopt the new versions for persisted trace and provider-capture changes, but must not introduce another independent bump. |
| Compatibility failures as explicit hard breaks | Workstream 2 | Workstream 3 | Workstream 2 owns host or loader compatibility errors. Workstream 3 owns persisted-state and trace-store compatibility behavior under the same version line. |
| Manual validation of trace context propagation and grouped traces | Workstreams 2 and 3 | Workstream 5 | Workstream 2 validates contract propagation, Workstream 3 validates grouped-finalization behavior, and Workstream 5 validates provider capture and rendering fidelity. |
| Manual validation of broader family selection and multi-task planning | Workstream 4 | None | Entirely benchmark-planning scope. |
| Focused regression tests for trace-context serialization and propagation | Workstream 2 | Workstreams 3, 5 | Workstream 2 covers request and runtime propagation. Workstream 3 covers grouped finalization from canonical records. Workstream 5 covers provider capture and wire-faithful rendering. |
| Focused regression tests for uncapped family selection, multi-task planning, and bounded synthesis provenance | Workstream 4 | None | Entirely benchmark-planning scope. |

### Quick Ownership Summary

- If you are implementing Workstream 2:
  focus on runtime-native request and execution contracts, trace-context propagation, normalized benchmark request identity, and the coordinated ABI and storage version bump.

- If you are implementing Workstream 3:
  focus on canonical raw trace storage, scoped finalization, rebuild-after-interruption, and durable grouped transcript generation. Do not redefine provider render semantics or benchmark-planning policy.

- If you are implementing Workstream 4:
  focus on goal-scoped benchmark planning, family scoring, task selection, coverage checks, bounded template synthesis, benchmark provenance, and factory-side trace stamping. Do not implement provider-wire rendering or grouped trace storage here.

- If you are implementing Workstream 5:
  focus on provider-side trace-context projection, raw request and response preservation, wire-faithful per-call rendering, and structured hosted-response capture. Do not take over benchmark planning or the grouped trace-finalization layer.
