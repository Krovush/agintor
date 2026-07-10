# Workstream 5: Tooling, Providers, and Control

## Outcome

- Tooling, provider, and control contracts become runtime-owned surfaces that live entirely inside the bundled solve-time runtime boundary.
- Promoted tools become durable runtime assets with manifests, validation evidence, determinism classification, sandbox fingerprints, and rollback state.
- Tool runs and provider background operations share a unified async job model with restart and receipt linkage.
- Hosted provider adapters preserve rich structured response envelopes instead of flattening runtime-visible results into text.
- Hosted-call records inside the Workstream 3 trace topology preserve provider-facing request payloads, response envelopes, retry/failover lineage, typed trace context, and wire-faithful per-call renders.
- Solve-time control decisions become predictor-backed, uncertainty-aware, and auditable from runtime traces, decision records, tool records, provider records, and Workstream 4 evidence.

## Inherited WS4 Context

Workstream 5 uses WS4 evaluation evidence to decide which runtime-owned tool, provider, and control contracts are worth hardening.

Relevant inherited context:

- typed benchmark adapter families, including serious `repo_patch` and `service_task` lanes;
- typed verifier evidence and held-out reports;
- `benchmark_provenance.json`, contamination records, validation history, stage-failure ledgers, leaderboard snapshots, and held-out reports;
- persisted archive, scheduler, operator, lineage, predictor, RNG, and search-resume state;
- signal-sufficiency reports that show whether predictor-backed solve-time decisions have enough full-train evidence to justify promotion;
- factory-side trace stamping for planning, mutation, patch repair, objective choice, and operator choice;
- Workstream 3 session-scoped trace topology, materialization manifests, grouped rebuild APIs, typed trace cursors, recovery records, and environment fingerprints.

Workstream 5 may extend runtime-owned schemas and provider/tool/control events. It must not redesign benchmark selection, verifier semantics, archive accounting, search-state persistence, or grouped trace topology.

## Boundaries

- Own runtime-side tool contracts, promoted-tool lifecycle, per-tool sandbox policy, tool execution lifecycle, hosted provider request and response contracts, provider retry and replay classification, async job records, and solve-time control behavior.
- Own provider-side projection of typed trace context into wire requests, raw hosted-call capture, per-call trace-record semantics, and wire-faithful per-call rendering.
- Own runtime-facing schemas such as:
  - tool manifests
  - promoted-tool records
  - sandbox policy
  - async job state
  - hosted response envelopes
  - decision records
- Keep runtime-wide branch scheduling, checkpoint timing, durable store implementation, benchmark policy, archive accounting, and export packaging outside this workstream.
- Keep factory-owned scheduler responsibilities out of solve-time control entirely.
- Keep session-scoped grouped trace finalization and long-lived trace-store indexing outside this workstream.
- Keep benchmark selection, verifier design, held-out reporting, contamination policy, signal-sufficiency accounting, and search-state persistence outside this workstream.
- Consume Workstream 3 `EnvironmentFingerprint`, `RecoveryAttempt`, `TraceCursorSnapshot`, side-effect receipts, async handle lineage, and grouped trace refs rather than introducing parallel recovery or trace stores.

## Non-Goals

- Replacing Agintor's internal runtime protocol with MCP
- Turning provider-native tool ecosystems into the canonical tool model
- Open-ended remote tool registries or cross-runtime tool sharing
- Broad multi-language generated-tool runtimes in the MVP
- Reopening Workstream 4 benchmark planning or search policy
- Reopening Workstream 3 trace-store topology or indexed state-store design

## Legacy Filters

Carry forward only legacy gaps that remain true for tooling, providers, and solve-time control after WS3 and WS4.

- Do carry forward durable promoted-tool assets: `agintor/tool_runtime.py` still mutates the in-memory registry more than it materializes exportable assets.
- Do carry forward stronger sandbox policy: current validation and content-addressed sandbox hashes are useful, but not yet a typed runtime-owned sandbox contract with filesystem, network, resource, user, and capability posture.
- Do carry forward unified async jobs: tool async handles exist and provider receipts exist, but tool runs and provider background work do not yet share one restartable `AsyncJobRecord`.
- Do carry forward rich hosted-response envelopes: `ModelResponse.text` is still the main runtime-facing projection, with hosted metadata mostly in `raw`.
- Do carry forward wire-faithful per-call rendering and provider capture richness inside the WS3 topology.
- Do carry forward predictor-backed solve-time utilities only where WS4 signal-sufficiency reports and predictor snapshots justify them.
- Do not carry forward claims that runtime control still owns factory scheduler or scope-credit methods; current `control_policy.py` is already limited to `assign_model`, `request_checks`, and `stop_policy`.
- Do not carry forward claims that factory/runtime profile separation is unresolved; WS5 only adds new runtime-owned fields and serialization rules.
- Do not carry forward flat-trace-topology work; Workstream 3 owns session layout, grouping, materialization state, and rebuild.
- Do not delete hosted providers, dynamic tool synthesis, or mutable surfaces as a "fix". Keep them bounded and harden their contracts.

## Baseline

- `agintor/tool_runtime.py` already has category-first discovery, generated-tool validation, sync execution, async handle launch, and task-local cleanup.
- Tool validation is already stronger than the old workstream docs implied, but promoted tools still behave more like registry mutations than durable runtime assets.
- `SandboxManager.sandbox_hash(...)` already gives a good content-addressed reuse foundation.
- `agintor/providers.py` already supports local deterministic, replay, retry, failover, OpenAI, and MiniMax providers.
- `provider_openai.py` already uses the Responses path, but the runtime-facing abstraction still collapses too much of the response into flat text.
- After Workstream 3, `openai_trace.py` owns the session-scoped trace topology, materialization manifest, grouped rebuild surface, and typed trace cursor links. Workstream 5 owns per-call capture fields and default wire-faithful rendering inside that topology.
- `control_policy.py` is already solve-time-only: `assign_model`, `request_checks`, and `stop_policy`. The remaining gap is utility quality and observability, not factory-scheduler leakage.
- The key current control heuristic hotspot is `_best_next_action_utility` and adjacent stop/check decision flow in `agintor/task_runtime/verification.py`.
- Runtime execution implementation now lives under `agintor/task_runtime/` with `agintor.runner.TaskRuntime` as a facade. Tool/provider/control changes must update bundled runtime-kernel source lists when new runtime-owned files are added.
- Runtime solve decisions still rely heavily on hand-written policy heuristics rather than shared predictor-backed utilities.
- The local tool registry is still canonical today. Remote MCP tools or provider-side tool search do not yet exist as explicit optional boundaries.

## Contract Inventory

- Make runtime-owned types explicit under the runtime boundary:
  - `HostedResponse`
  - `HostedResponseItem`
  - `ToolManifest`
  - `PromotedToolRecord`
  - `ToolSandboxPolicy`
  - `AsyncJobRecord`
  - `DecisionRecord`
  - `RetryClassification`
  - `FailoverRecord`

These contracts belong in `agintor/schemas.py` or adjacent runtime-contract modules that are bundled by `agintor/runtime_sdk/bundle.py`. They must be available to exported runtimes without importing factory-only modules.

## Core Decisions

- Keep the local deterministic provider mandatory and the replay provider first-class.
- Use rich hosted-response envelopes as the primary hosted-provider abstraction. Flat text remains a convenience view only.
- Keep MCP and remote tool interoperability optional at the tool edge, not as the runtime's internal protocol.
- Distinguish task-local tools from promoted reusable tools explicitly in runtime state and on-disk asset layout.
- Promote only deterministic or environment-deterministic tools in the MVP. Replayable nondeterministic tools may remain task-local. Side-effectful tools remain non-promotable unless later work explicitly broadens that boundary.
- Unify tool handles and provider background jobs under one async job model tied to receipts, recovery, and environment fingerprints.
- Treat `metadata.mode` as purpose classification only. Cross-cutting correlation data lives in `trace_context`.
- Project canonical request-side trace context into provider metadata immediately before dispatch. Provider adapters must preserve it exactly and omit missing fields rather than synthesizing them.
- Make wire-faithful request and response rendering the default per-call trace view. Verbose debug rendering remains opt-in and clearly separate.
- Use `request_payload` as the canonical outgoing render source and preserved hosted response envelopes as the canonical incoming render source.
- Preserve `provider_role` exactly as supplied by upstream runtime and factory contracts, using only `factory` and `runtime`. Provider adapters do not infer or rename role values.

## Phase 1: Upgrade Existing Bundled Runtime Contracts In Place

- The runtime boundary already exists and is bundled. Upgrade the existing solve-time schemas and logic in place, including:
  - tool specs
  - tool execution results
  - hosted provider request and response envelopes
  - async job records
  - sandbox policy
  - control decision records
- Remove remaining solve-time dependence on factory-side implementation classes. Runtime-kernel implementation may live in `agintor/task_runtime/`, `agintor/tool_runtime.py`, provider modules, and runtime-contract modules that are bundled into `agintor_runtime`.
- Prioritize actual solve-time call sites:
  - `agintor/task_runtime/tooling.py`
  - `agintor/task_runtime/operations.py`
  - `agintor/task_runtime/verification.py`
  - `agintor/task_runtime/execution_loop.py`
  - `agintor/runtime_api.py`
- Any new runtime-owned module must be added to `_KERNEL_SOURCE_FILES` in `agintor/runtime_sdk/bundle.py`, and exported runtimes must still import `.runner.TaskRuntime` through the bundled package facade.
- Rehome provider-side trace-context projection with the runtime-owned request path so hosted adapters receive typed correlation data without making provider metadata the canonical owner of the contract.
- Freeze solve-time control to:
  - `assign_model`
  - `request_checks`
  - `stop_policy`
- Keep factory scheduler logic out of runtime control permanently.
- Add only runtime-owned fields to `runtime_profile.json`. Do not reopen the already-existing logical split between `runtime_profile_payload(...)` and `factory_profile_payload(...)`.
- Treat WS4 predictor snapshots and signal-sufficiency reports as read-only inputs. Workstream 5 may consume frozen predictor state at solve time, but retraining, label harvesting, archive credit, and operator policy remain factory-side.

## Phase 2: Materialize the Promoted-Tool Lifecycle

- Add an on-disk promoted-tool asset format that includes at least:
  - tool manifest
  - source digest
  - validation evidence
  - determinism class
  - permission scope
  - sandbox fingerprint
  - version metadata
  - provenance
  - state
  - reuse evidence count
- Use explicit states such as:
  - `active`
  - `quarantined`
  - `disabled`
  - `rolled_back`
- Add determinism classes:
  - `pure_deterministic`
  - `environment_deterministic`
  - `replayable_nondeterministic`
  - `side_effectful`
- Promotion rules for the MVP:
  - allow `pure_deterministic`
  - allow `environment_deterministic`
  - keep `replayable_nondeterministic` task-local
  - keep `side_effectful` non-promotable
- Require repeated success across distinct tasks before promotion.
- Persist promoted-tool assets under the exported runtime artifact, not in the factory archive. Export bundles and provenance bundles must reference the promoted-tool manifest digests without embedding factory search history.
- Link promotion evidence to WS4 verifier evidence and WS3 receipt, checkpoint, and environment-fingerprint refs.

## Phase 3: Harden Per-Tool Sandbox Policy

- Introduce a typed `ToolSandboxPolicy` that covers:
  - runtime or backend
  - dependency digest or lock data
  - filesystem policy
  - mount policy
  - network policy
  - timeout
  - CPU ceiling
  - memory ceiling
  - PID limit
  - required permissions
  - user, capability, and seccomp settings where supported
- Make the default sandbox posture:
  - read-only root
  - tmpfs scratch
  - explicit writable mounts only
  - no network
  - non-root
  - cap-drop-all
  - bounded CPU, memory, and PID use
- Keep platform-specific hardening behind typed policy rather than implicit subprocess behavior.
- Degrade explicitly when a backend cannot enforce a policy feature. Record the unsupported guarantees in the tool manifest, environment fingerprint, async job record, and runtime trace rather than silently pretending hardening exists.
- Do not make Docker or OS-level hardening mandatory for all default tests. Default tests must remain deterministic and local while still validating policy serialization and fail-closed behavior.

## Phase 4: Unify Tool Handles and Provider Background Jobs

- Add a shared `AsyncJobRecord` for both tool and provider background operations.
- `AsyncJobRecord` must carry at least:
  - job ID
  - job kind
  - owner branch
  - backend
  - lifecycle state
  - cancel support
  - resume policy
  - receipt linkage
  - environment fingerprint
  - result refs
- Extend runtime execution with first-class lifecycle operations:
  - launch
  - poll
  - await
  - cancel
  - timeout
  - orphan cleanup
  - crash recovery
- Persist enough state that the Workstream 3 recovery and receipt paths can restore or reconcile both tool and provider jobs through one model.
- Preserve the existing `AsyncHandle` ABI until it is replaced by or nested inside `AsyncJobRecord` in one intentional schema change. Do not maintain two unrelated background-work authorities.

## Phase 5: Modernize Hosted Provider Adapters

- Refactor hosted provider adapters around a richer `HostedResponse` contract that preserves:
  - response ID
  - model identity
  - status
  - output item array
  - structured-output payloads
  - previous-response or conversation linkage
  - tool-call items and linkage IDs
  - usage
  - latency
  - cost
  - retry and failover lineage
  - background state
  - normalized streaming event log
- Keep flat text as a convenience projection, not the primary contract.
- Replace the text-first runtime-facing contract intentionally. Do not layer new behavior on top of ad-hoc `ModelResponse.raw` dictionaries as the primary hosted-response API.
- Use pinned snapshot model IDs by default for evaluation and replay lanes.
- Persist `trace_context` as a first-class field beside `request_metadata`, `request_payload`, and `response_raw`.
- Use the Workstream 3 resolved trace-context helper and session-scoped call store. Workstream 5 may add per-call fields, but it must not create another trace root, materialization manifest, grouping key, or rebuild cursor.
- For OpenAI Responses adapters, preserve enough raw request-envelope fields for wire-faithful rendering, including:
  - `instructions`
  - `input`
  - `model`
  - `reasoning`
  - `max_output_tokens`
- Default human-readable per-call traces must:
  - render `Outgoing` from wire payload fields only
  - render `Incoming` from `response_raw.output` message items before falling back to flat text
  - suppress local metadata payloads in the visible API body
  - show `trace_context`, purpose, tokens, latency, and provider role in a separate `Call Context` section
- `calls/*.md` must follow the same wire-faithful rules as grouped transcripts.
- Keep optional verbose render mode that includes local orchestration metadata for debugging without changing the default trace readability.
- Replace generic exception-text handling with typed retry and failover classes:
  - auth or config
  - rate limit
  - transient network
  - overload
  - invalid request
  - non-retryable contract parse
- Link provider background jobs into the unified `AsyncJobRecord` model instead of treating them as adapter-local state.
- Apply the same hosted-response envelope and retry/failover classification to OpenAI and MiniMax adapters, with provider-specific raw payload preservation and one shared runtime-facing contract.

## Phase 6: Put Solve-Time Decisions on Shared Utility Models

- Add explicit feature extraction for solve-time families such as:
  - category ranking
  - tool ranking
  - build versus reuse
  - model assignment
  - check selection
  - stop policy
- Use shared decision utilities with two heads:
  - conservative utility for irreversible or externally visible actions
  - exploratory utility for optional probes and speculative actions
- Replace local heuristic constants in runtime decision flow with predictor-backed utilities plus deterministic fallback.
- Emit decision records containing:
  - selected action
  - estimated utility
  - uncertainty
  - fallback reason
  - discarded alternatives
- Keep predictor retraining policy, label harvesting, archive credit, and signal-sufficiency decisions outside this workstream. This workstream consumes frozen predictor state at solve time.
- If WS4 reports insufficient signal for a decision family, keep the deterministic heuristic fallback as the active path and emit `DecisionRecord.fallback_reason="insufficient_search_signal"`.
- Decision records must link to trace context, task or solve request identity, selected action, discarded alternatives, estimated utility, uncertainty, fallback reason, and supporting predictor snapshot ID.
- New `DecisionRecord`, `HostedResponse`, and `AsyncJobRecord` artifacts must link into the existing provenance backbone: `RuntimeEvent`, `SideEffectReceipt`, checkpoint refs, recovery attempt refs, environment fingerprint refs, hosted call IDs, and grouped trace refs. They must not create a parallel audit lane.

## Optional External Tool Interop

- Keep the local tool registry canonical.
- Add optional, bounded interop surfaces for:
  - remote MCP tools
  - connectors
  - provider-side dynamic tool search where supported
- Apply the same category-first discovery and permission checks to remote or provider-side tools that local tools already obey.
- Keep deterministic local fallback paths for providers or environments that do not support these integrations.
- Do not let external tool interop become the internal runtime protocol.

## Phase 7: Tighten Observability and Runtime Tests

- Add stable structured events for:
  - category selection
  - reusable-tool ranking
  - synthesis proposal
  - validation outcome
  - promotion or quarantine
  - sandbox launch
  - async job transition
  - provider request
  - retry
  - failover
  - model assignment
  - check request
  - stop reason
- Events should point to canonical WS3 refs where available: receipt IDs, checkpoint refs, environment fingerprint IDs, recovery attempt IDs, async job IDs, hosted call IDs, and grouped trace refs.
- Keep live-provider tests opt-in and bounded.
- Keep the default regression lane local or replay-backed.
- Add deterministic tests for:
  - promoted-tool reload and quarantine
  - sandbox policy enforcement
  - async job recovery
  - hosted response capture
  - provider replay
  - retry and failover classification
  - MCP and tool-search boundary handling
  - predictor fallback behavior
  - bundled runtime imports for any new runtime-owned schema or helper modules
  - per-call wire-faithful rendering from preserved request payload and hosted response envelopes inside the WS3 trace topology

## Acceptance Criteria

1. Exported runtimes carry a self-contained tool, provider, and control layer under the runtime boundary.
2. Solve-time control contains no factory scheduler hooks.
3. Promoted tools are real runtime assets with manifests, validation evidence, determinism classification, and rollback state.
4. Every tool run and provider background operation executes through an explicit async job and sandbox contract.
5. Hosted provider responses preserve structured runtime-visible metadata instead of collapsing to text only.
6. Optional MCP and provider-side tool-search interop remain bounded extensions rather than the internal runtime architecture.
7. Tooling and control decisions use shared utility estimates with deterministic fallback and emit decision records.
8. Runtime traces and persisted artifacts explain tool, provider, sandbox, and control behavior from the workspace alone.
9. Hosted provider call records persist first-class trace context and wire-faithful per-call renders without mixing local metadata into the visible API conversation.
10. OpenAI Responses adapters preserve enough structured raw output to reconstruct visible message bodies without defaulting to flattened text.
11. MiniMax and other hosted adapters preserve the same runtime-facing `HostedResponse` semantics even when their native raw payload shapes differ.
12. New runtime-owned contracts are included in exported runtime kernel bundles and import successfully from `agintor_runtime`.
13. Predictor-backed runtime decisions are enabled only for families with sufficient WS4 evidence; otherwise deterministic fallbacks remain active and auditable.
14. Promoted-tool manifests, async job records, hosted-call records, and decision records link back to WS3 recovery, receipt, environment, and trace refs without creating parallel stores.

## File Ownership

- `agintor/tool_runtime.py`: tool registry, validation pipeline, promoted-tool lifecycle, sandbox policy, async job integration
- `agintor/providers.py`: provider assembly, retry and failover policy, replay wiring, transport adapters
- `agintor/provider_common.py`: shared provider contracts and deterministic or replay providers
- `agintor/provider_openai.py`: Responses-native hosted adapter and rich hosted-response capture
- `agintor/provider_minimax.py`: hosted adapter alignment with shared response contracts
- `agintor/openai_trace.py`: per-call trace-record schema, wire-faithful rendering, and provider-facing capture inside the WS3-owned session topology
- `agintor/predictors.py`: solve-time utility helpers and predictor-backed decision surfaces
- `agintor/runtime_sdk/`: runtime-owned tool, provider, and control execution surfaces plus bundled kernel source list updates
- `agintor/runtime_profile.py`: runtime-owned provider, tooling, and control profile fields plus serialization rules
- `agintor/templates/baseline_runtime/tool_policy.py`: solve-time tool ranking, build-versus-reuse, validation opinion, promotion decision
- `agintor/templates/baseline_runtime/control_policy.py`: solve-time model assignment, checker request, stop policy
- `agintor/schemas.py` or adjacent runtime-contract modules: tool manifests, hosted responses, sandbox policy, async job records, decision records
- `agintor/task_runtime/tooling.py`, `operations.py`, `verification.py`, and `execution_loop.py`: tool/provider/control execution integration points only; do not add factory search or benchmark logic here
- `agintor/evolution.py`, `agintor/evaluator.py`, `agintor/archive.py`, and `agintor/runtime_builder.py`: consumed as WS4 evidence producers only; Workstream 5 must not move their responsibilities into runtime policy
- `tests/test_core.py`, `tests/test_runtime_spec.py`, `tests/test_live_openai.py`, and adjacent new tests: promoted-tool lifecycle, provider transport, async-job, sandbox, and control regression coverage

## Deferred

- Multi-language generated-tool runtimes beyond the bounded Python-first lane
- Remote promoted-tool registries
- Dynamic live price or region routing across hosted providers
- Rich streaming UIs beyond structured runtime artifacts
