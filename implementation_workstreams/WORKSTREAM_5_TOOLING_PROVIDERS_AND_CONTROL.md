# Workstream 5: Tooling, Providers, and Control

## Outcome

- Tooling, provider, and control contracts become runtime-owned surfaces that live entirely inside the bundled solve-time runtime boundary.
- Promoted tools become durable runtime assets with manifests, validation evidence, determinism classification, sandbox fingerprints, and rollback state.
- Tool runs and provider background operations share a unified async job model with restart and receipt linkage.
- Hosted provider adapters preserve rich structured response envelopes instead of flattening everything into text.
- Hosted-call records preserve typed trace context and wire-faithful request and response renders instead of mixing transport payloads with local orchestration metadata.
- Solve-time control decisions become predictor-backed, uncertainty-aware, and auditable from runtime traces and workspace artifacts.

## Prerequisites

- Workstream 1 freezes the runtime boundary and export contract.
- Workstream 2 freezes orchestration, checkpoint, and receipt semantics.
- Workstream 3 provides durable state, replay, and recovery records.
- Workstream 4 provides serious held-out evidence and resumable search state.

## Sequence Position

- This workstream starts after Workstream 1 freezes the runtime boundary, Workstream 2 freezes orchestration and receipt semantics, Workstream 3 provides durable state, and Workstream 4 provides serious evaluation signals.
- This workstream is last on purpose. Tool/provider/control modernization should consume a stable runtime architecture and real evaluation evidence instead of moving targets.

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

## Non-Goals

- Replacing Agintor's internal runtime protocol with MCP
- Turning provider-native tool ecosystems into the canonical tool model
- Open-ended remote tool registries or cross-runtime tool sharing
- Broad multi-language generated-tool runtimes in the MVP

## Baseline

- `agintor/tool_runtime.py` already has category-first discovery, generated-tool validation, sync execution, async handle launch, and task-local cleanup.
- Tool validation is already stronger than the old workstream docs implied, but promoted tools still behave more like registry mutations than durable runtime assets.
- `SandboxManager.sandbox_hash(...)` already gives a good content-addressed reuse foundation.
- `agintor/providers.py` already supports local deterministic, replay, retry, failover, OpenAI, and MiniMax providers.
- `provider_openai.py` already uses the Responses path, but the runtime-facing abstraction still collapses too much of the response into flat text.
- `openai_trace.py` still mixes local metadata into human-facing transcripts and does not persist `trace_context` as a first-class stored field.
- `control_policy.py` still carries factory-owned methods that do not belong to solve-time control.
- `runner.py` still relies too heavily on hand-written decision heuristics rather than shared predictor-backed utilities.
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

## Phase 1: Move Tooling, Provider, and Control Contracts into the Runtime Boundary

- Rehome runtime-owned solve-time schemas and logic under the bundled runtime boundary, including:
  - tool specs
  - tool execution results
  - hosted provider request and response envelopes
  - async job records
  - sandbox policy
  - control decision records
- Remove remaining solve-time dependence on host-side implementation classes.
- Rehome provider-side trace-context projection with the runtime-owned request path so hosted adapters receive typed correlation data without making provider metadata the canonical owner of the contract.
- Freeze solve-time control to:
  - `assign_model`
  - `request_checks`
  - `stop_policy`
- Keep factory scheduler logic out of runtime control permanently.
- Keep `runtime_profile.json` limited to solve-time provider, tooling, and control settings only.

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
- Persist enough state that Workstream 3 can restore or reconcile both tool and provider jobs through one model.

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
- Use pinned snapshot model IDs by default for evaluation and replay lanes.
- Persist `trace_context` as a first-class field beside `request_metadata`, `request_payload`, and `response_raw`.
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
- Keep predictor retraining policy and label harvesting outside this workstream. This workstream consumes frozen predictor state at solve time.

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

## Acceptance Gates

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

## File Ownership

- `agintor/tool_runtime.py`: tool registry, validation pipeline, promoted-tool lifecycle, sandbox policy, async job integration
- `agintor/providers.py`: provider assembly, retry and failover policy, replay wiring, transport adapters
- `agintor/provider_common.py`: shared provider contracts and deterministic or replay providers
- `agintor/provider_openai.py`: Responses-native hosted adapter and rich hosted-response capture
- `agintor/provider_minimax.py`: hosted adapter alignment with shared response contracts
- `agintor/openai_trace.py`: per-call trace-record schema, wire-faithful rendering, and provider-facing trace persistence
- `agintor/predictors.py`: solve-time utility helpers and predictor-backed decision surfaces
- `agintor/runtime_sdk/`: runtime-owned tool, provider, and control execution surfaces
- `agintor/runtime_profile.py`: runtime-owned provider, tooling, and control profile fields plus serialization rules
- `templates/baseline_runtime/tool_policy.py`: solve-time tool ranking, build-versus-reuse, validation opinion, promotion decision
- `templates/baseline_runtime/control_policy.py`: solve-time model assignment, checker request, stop policy
- `agintor/schemas.py` or adjacent runtime-contract modules: tool manifests, hosted responses, sandbox policy, async job records, decision records
- `tests/test_core.py`, `tests/test_runtime_spec.py`, `tests/test_live_openai.py`, and adjacent new tests: promoted-tool lifecycle, provider transport, async-job, sandbox, and control regression coverage

## Deferred

- Multi-language generated-tool runtimes beyond the bounded Python-first lane
- Remote promoted-tool registries
- Dynamic live price or region routing across hosted providers
- Rich streaming UIs beyond structured runtime artifacts
