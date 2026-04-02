# Workstream 5: Tooling, Providers, And Control

## Outcome

- The runtime tool layer must become a bounded capability system with clear separation between task-local generated tools and promoted reusable tools. Promoted tools must have stable manifests, validation evidence, sandbox fingerprints, and runtime-owned loading paths.
- Per-tool execution must become an explicit runtime contract. Tool runs and background jobs must expose sandbox policy, permission scope, resource ceilings, lifecycle state, and failure reasons instead of behaving like anonymous subprocesses.
- The provider layer must become a stable runtime-facing request and response contract that can support local deterministic execution, replay, and hosted providers without collapsing everything into flat text strings.
- The control surface must become purely solve-time and predictor-informed. Model assignment, checker requests, and stopping must be explainable from runtime state, uncertainty estimates, and budget state, not mixed with factory scheduler responsibilities.
- Execution-time observability must make it possible to answer, from workspace artifacts alone, which tool was selected or synthesized, why a provider was chosen or retried, what hosted response metadata was returned, and why the runtime asked for checks or stopped.

## Boundaries

- Own the runtime-side tool lifecycle, generated tool validation, promoted-tool materialization, per-tool sandbox policy, async tool-job lifecycle, provider adapters and wrappers, runtime-side provider environment handling, predictor utility plumbing, and solve-time control behavior inside the runtime execution layer.
- Own the runtime-facing schemas and manifests for `ToolSpec`, `AsyncHandle`, tool execution results, provider requests and responses, promoted-tool records, and any adjacent sandbox or job-state contracts introduced for this workstream.
- Keep runtime-wide container isolation, branch scheduling, checkpoint timing, and request execution flow outside this workstream. This workstream owns per-tool isolation and provider/runtime execution semantics, not the global runtime scheduler.
- Keep durable storage, checkpoint persistence, replay indexing, and long-term memory durability outside this workstream. This workstream defines which tool and provider records must be serializable, but not the storage engine.
- Keep benchmark checker ladders, predictor label extraction policy, archive scoring, scope credit, and mutation acceptance outside this workstream. This workstream consumes frozen signals at solve time; it does not own search accounting.
- Keep build-time provider planning, export bundle wiring, deployment-contract packaging, and CLI solve surface changes outside this workstream. This workstream owns the runtime-owned tool and provider assets consumed at solve time.

## Non-Goals

- Do not turn the MVP into an unbounded package manager, external-service mesh, or general tool marketplace.
- Do not expand the mutable runtime search surface beyond the existing tool and control policy files plus adjacent runtime-owned support modules.
- Do not let the exported runtime own archive credit, objective selection, benchmark planning, or any other factory scheduler state.
- Do not require full hosted-provider feature parity before the local, replay, and primary OpenAI path are stable.
- Do not make multi-language tool synthesis part of the MVP critical path. The first milestone is a durable, bounded, inspectable tool system with stable runtime contracts.

## Baseline

- `agintor/tool_runtime.py` provides a tool lifecycle: category-first discovery inputs, built-in registry entries, generated tool registration, validation, synchronous execution, async handle launch, waiting, and task-local tool reset.
- Generated tools undergo validation: permission checks, AST and import checks, signature validation, `py_compile`, import-resolution checks, timeout runs, smoke tests, and deterministic replay trials.
- Sandbox reuse is content-addressed through `SandboxManager.sandbox_hash(...)`, which hashes tool source identity, runtime, dependencies, permissions, base image inputs, and test digests.
- The tool safety model is lightweight. `SafetyGuard` is primarily static AST and import filtering plus a permission denylist, not a hardened execution boundary with explicit filesystem, network, CPU, memory, or process controls.
- Async tool handles are runtime objects, but `runner.py` usually launches a handle and waits on it immediately. That limits overlap and makes background work more constrained than its interface suggests.
- Generated tools are task-local Python assets under `generated/local`. Promotion updates the in-memory registry, but promoted tools do not become stable runtime-owned assets with manifests, rollback state, or export hooks.
- `agintor/templates/baseline_runtime/control_policy.py` implements `assign_model`, `request_checks`, and `stop_policy`, and also carries `score_interface_scope` and `update_scope_credit`, which are factory-owned responsibilities.
- `agintor/predictors.py` contains a `DecisionFamilyModelBank`, bootstrapped probability models, positive-value models, ranking mixers, freezing, and retraining thresholds, but solve-time control and tooling rely mainly on heuristics.
- `runner.py` computes best-next-action utility with hand-written constants instead of routing decisions through shared predictor utilities and uncertainty-aware action scoring.
- `agintor/providers.py` supports local deterministic, replay, retry, failover, OpenAI, and MiniMax providers. Provider payloads serialize across Docker boundaries, including mounted key files and replay files.
- `agintor/provider_openai.py` uses the Responses client, supports reasoning-effort mapping, max-output-token handling, and cost accounting, but the runtime-facing contract flattens hosted responses down to `ModelResponse.text` plus a small raw metadata map.
- `runner.py` scrubs unrelated provider environment variables before runtime execution, which separates runtime-visible secrets from unrelated host configuration.
- `container_runtime.py` serializes provider payloads and mounted files into Docker execution, but the provider contract is shaped around simple buffered requests and responses rather than a richer hosted response envelope.
- Tooling and provider execution depend on a solve-time kernel that lives in the Agintor package. Exported runtimes do not ship a self-contained runtime-owned tool or provider layer that can execute without importing host implementation modules.

## Contract Decisions

- Use a Responses-style hosted-provider contract as the primary abstraction for hosted models. The runtime may expose a flat-text adapter, but hosted adapters must preserve richer response structure.
- Preserve structured-output metadata, request identifiers, reasoning settings, itemized tool-call records, usage, latency, and cost in runtime-visible provider responses even when the immediate consumer only needs text.
- Treat tool and provider contracts as runtime-owned protocol payloads. The host may transport them, but their semantics, validation, and execution must live in the runtime execution layer rather than in shared host modules.
- Treat pinned snapshot model identifiers as the default for evaluation and replay paths when hosted providers are used. Aliases may remain opt-in for interactive or development runs, but evaluation must prefer stable model identities.
- Keep the local deterministic provider mandatory and the replay provider first-class. The runtime factory must be able to run bounded search and regression tests without live hosted-model dependence.
- Keep tool synthesis bounded and inspectable. The MVP should strengthen tool manifests, sandbox policy, and promotion durability before broadening the runtime or language surface of generated tools.

## Phase 1: Rehome Tooling And Provider Contracts Into The Runtime Boundary

- Move runtime-owned tool and provider execution contracts behind the runtime entry interface.
- Ensure the solve-time runtime kernel includes the provider adapters, tool registry, sandbox policy types, and control-surface contracts required to execute solves without importing host implementation modules.
- Separate transport concerns from runtime execution concerns. The host may pass request payloads, mounted file references, and environment allowlists, but the runtime kernel must own interpretation and execution of tool and provider contracts.
- Make runtime-owned manifests and schemas self-sufficient so exported runtimes can validate promoted tools, background handles, and hosted-provider responses on a fresh machine.
- Keep explicit adapters at the boundary rather than hidden dependencies on host-side classes.

`Exit gate:` exported runtimes carry a self-contained runtime-owned tool and provider layer, and the host is not the implicit implementation of those solve-time contracts.

## Phase 2: Remove Factory Leakage And Freeze Runtime-Owned Contracts

- Remove `score_interface_scope` and `update_scope_credit` from `agintor/templates/baseline_runtime/control_policy.py`.
- Update `agintor/prompt_builder.py` so the `ctl` mutator contract contains only solve-time methods: `assign_model`, `request_checks`, and `stop_policy`.
- Expand or split runtime-owned contracts in `agintor/schemas.py` so this workstream has explicit types for:
  hosted provider requests,
  hosted provider response envelopes,
  promoted tool manifests,
  sandbox policy,
  and background-job state.
- Provide explicit adapters from flat `ModelRequest` and `ModelResponse` paths while the richer contract lands.
- Keep `runtime_profile.json` runtime-owned for provider, tooling, and control settings only. Factory planning and evaluation knobs remain outside this workstream.

`Exit gate:` the exported runtime control surface contains only solve-time behavior, and the runtime-owned tool and provider contracts are explicit enough that later phases stop overloading `ModelResponse.text` and ad hoc registry state.

## Phase 3: Materialize The Promoted-Tool Lifecycle

- Add a promoted-tool asset format that includes at least:
  tool manifest,
  source file digest,
  validation results,
  determinism class,
  permission set,
  sandbox fingerprint,
  version metadata,
  provenance,
  and rollback or disable state.
- Distinguish task-local generated tools from promoted reusable tools in both runtime state and on-disk asset layout.
- Make promoted tools loadable as runtime-owned assets during runtime startup without reopening factory history or evaluation traces.
- Keep promotion conservative:
  local validation must pass,
  determinism must be stable,
  safety checks must pass,
  and repeated distinct-task reuse must be required before promotion.
- Add rollback or quarantine semantics for promoted tools that later fail validation, drift, or fall outside the runtime contract.
- Coordinate asset references with export hooks and persistence surfaces, but keep the manifest and loading semantics owned here.

`Exit gate:` a promoted tool can survive beyond the task that created it, reload as a runtime-owned asset, and carry enough validation and provenance metadata to be inspected or disabled without reopening code.

## Phase 4: Harden Per-Tool Sandbox Policy And Background Jobs

- Introduce an explicit per-tool sandbox policy object covering:
  runtime,
  dependency digest or lock data,
  filesystem policy,
  network policy,
  timeout,
  CPU and memory ceilings,
  process limits,
  mount policy,
  and required permissions.
- Keep the first implementation bounded. The MVP needs a pluggable sandbox interface with a strong local baseline and optional container-backed execution where available, not a platform-specific syscall framework in the first pass.
- Extend `ToolExecutor` so background jobs have first-class lifecycle operations:
  launch,
  poll,
  await,
  cancel,
  timeout,
  orphan cleanup,
  and crash-state reporting.
- Expose non-blocking handle lifecycle primitives to the orchestration layer. This workstream must stop forcing immediate wait semantics as the only practical path.
- Record environment and sandbox fingerprints from real tool execution so persisted runtime state and exported runtime assets can refer to them directly.
- Tighten failure semantics so tool faults, sandbox policy failures, timeout failures, and background-job recovery failures are distinguishable in traces and results.

`Exit gate:` tool execution always runs through an explicit sandbox and job-state contract, and every handle or tool run leaves behind enough structured metadata to explain what backend ran, what policy was applied, and why it succeeded or failed.

## Phase 5: Modernize Hosted Provider Adapters And Replay

- Refactor the provider layer around a richer hosted response contract instead of a text-only abstraction.
- Preserve itemized hosted response data such as:
  structured-output payloads,
  request IDs,
  response status,
  reasoning settings,
  tool-call items,
  tool-call linkage IDs,
  usage,
  latency,
  and cost.
- Keep `OpenAIProvider` centered on the Responses API path. Chat-style flat responses may exist as a fallback transport, not the primary abstraction.
- Add a streaming-ready interface even if the CLI initially consumes buffered text. Replay capture should be able to store normalized stream or item events so hosted behavior can be reconstructed without live API access.
- Replace generic exception-text retry and failover heuristics with provider-aware classification of:
  auth or configuration errors,
  rate limits,
  transient network failures,
  provider overload,
  deterministic bad requests,
  and non-retryable contract errors.
- Strengthen provider observability so retries, failover hops, health state, and replay lineage become runtime-visible structured artifacts instead of remaining local wrapper details.
- Support container payload serialization with mounted key files, replay files, and provider environment allowlists, and continue avoiding live secret embedding in runtime-owned artifacts.

`Exit gate:` hosted-provider execution and replay capture preserve enough response structure to debug a run, compare providers, and reproduce containerized execution without reducing everything to raw prompt text and flat output strings.

## Phase 6: Move Tooling And Control Onto Predictor Utilities

- Add explicit feature extraction for:
  tool category ranking,
  tool ranking,
  build-vs-reuse decisions,
  model assignment,
  checker selection,
  and stop policy.
- Route irreversible or externally visible decisions through conservative utility estimates, and route exploratory or optional actions through optimistic utility estimates with uncertainty penalties or bonuses.
- Use `DecisionFamilyModelBank` as the shared utility engine for tooling and control decisions, with deterministic heuristic fallback when observation counts are too low or the predictor family is untrained.
- Move `runner.py` off the current hand-written next-action utility constants and onto shared predictor-backed utility helpers.
- Add calibration summaries, uncertainty diagnostics, and family-level observation reporting so the runtime can explain when it trusted learned estimates versus when it fell back to heuristics.
- Keep predictor freeze behavior intact during evaluation. This workstream owns solve-time consumption of frozen predictor state, not label harvesting or retraining cadence.

`Exit gate:` runtime traces and decision records show utility, uncertainty, and fallback reason for major tooling and control decisions, and solve-time behavior is driven by shared utility estimates rather than ad hoc local constants.

## Phase 7: Tighten Execution-Time Observability And Test Coverage

- Add stable trace events for:
  category slice selection,
  reusable tool ranking,
  tool synthesis proposal,
  validation outcome,
  promotion decision,
  sandbox launch,
  handle-state transitions,
  provider request,
  retry,
  failover,
  model assignment,
  check request,
  and stop reason.
- Keep those events bounded and structured so they are useful to CLI users, runtime debugging, and evaluator reporting without turning traces into unbounded logs.
- Expand targeted tests across:
  promoted-tool manifests,
  sandbox policy enforcement,
  handle lifecycle,
  provider payload serialization,
  hosted response contract behavior,
  retry and failover classification,
  predictor utility fallback behavior,
  and control-surface cleanup.
- Keep live-provider tests opt-in and bounded. The default regression path must stay local and replay-capable.

`Exit gate:` the runtime leaves behind enough structured evidence to explain tool, provider, sandbox, and control decisions without reopening source code, and the main failure paths are covered by deterministic tests.

## MVP Acceptance Sequence

1. Exported runtimes carry a self-contained runtime-owned tool and provider layer rather than depending on host implementation modules for solve-time execution.
2. The exported runtime control surface contains only solve-time methods and no factory scheduler hooks.
3. Generated tools remain task-local unless they pass promotion requirements and are materialized as runtime-owned promoted-tool assets with manifests and validation metadata.
4. Every tool run and background job executes through an explicit sandbox and lifecycle contract with auditable permissions, policy, and failure state.
5. Hosted provider adapters preserve structured response metadata, request identity, reasoning settings, usage, and replayable item records across local and Docker execution without embedding secrets.
6. Tooling and control decisions consume shared predictor utilities with deterministic fallback when families are cold or untrained.
7. Runtime traces and workspace artifacts clearly expose tool, provider, sandbox, and control decisions well enough to debug a run without re-reading the implementation.

## File Ownership

- `agintor/tool_runtime.py`: tool registry, generated tool materialization, validation pipeline, sandbox policy and backend plumbing, tool execution, background-job lifecycle.
- `agintor/providers.py`: provider assembly, retry and failover wrappers, payload serialization, replay wiring, environment-name discovery, provider observability surfaces.
- `agintor/provider_common.py`: runtime-neutral provider base classes, request and response adapters, local deterministic provider, pricing and accounting helpers.
- `agintor/provider_openai.py`: Responses-native OpenAI adapter, reasoning-effort handling, structured hosted response capture, snapshot-model defaults for evaluation.
- `agintor/provider_minimax.py`: MiniMax adapter and alignment with the shared hosted-provider contract.
- `agintor/runtime_sdk/` or equivalent bundled solve-time kernel package: runtime-owned tool, provider, and control execution surfaces shipped with exported runtimes.
- `agintor/predictors.py`: shared predictor bank, uncertainty and utility helpers, calibration summaries, solve-time predictor consumption surfaces.
- `agintor/runtime_profile.py`: runtime-owned provider, tooling, and control profile fields plus serialization rules.
- `agintor/schemas.py` or adjacent runtime-owned contract modules: `ToolSpec`, `AsyncHandle`, tool execution results, provider request and response envelopes, promoted-tool manifests, sandbox or job-state records.
- `agintor/templates/baseline_runtime/tool_policy.py`: solve-time category ranking, tool ranking, build-vs-reuse, validation opinion, promotion decision, dispatch metadata.
- `agintor/templates/baseline_runtime/control_policy.py`: solve-time model assignment, checker requests, and stop policy only.
- `agintor/runner.py`: integration points for tool selection, sandbox and handle lifecycle, provider response handling, predictor-backed utility use, and execution-time observability.
- `agintor/container_runtime.py` and `agintor/container_entry.py`: provider payload rehydration, mounted key-file paths, replay files, and runtime-visible hosted-provider contract transport across Docker execution.
- `tests/test_algorithms.py`, `tests/test_core.py`, `tests/test_runtime_spec.py`, `tests/test_live_openai.py`, and adjacent targeted new tests: deterministic tool and control behavior, provider transport, replay behavior, and live hosted smoke checks.

## Deferred Until Post-MVP

- Multi-language generated-tool runtimes beyond the bounded Python-first tool path.
- Remote promoted-tool registries, signed tool packages, and cross-runtime sharing infrastructure.
- Adaptive multi-provider routing based on live regional health or price arbitration.
- Rich streaming UIs or long-running provider dashboards beyond structured workspace artifacts.
- Provider-specific native tool ecosystems beyond the shared hosted-provider contract once the core runtime abstraction is stable.
