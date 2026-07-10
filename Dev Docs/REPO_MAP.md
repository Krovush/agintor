# Repo Map

Reconciled with the live checkout on 2026-07-09.

Scope: this index includes files that are useful for understanding, implementing, or adapting Agintor. It excludes tests, temp workspaces, cache folders, generated run/trace artifacts, bytecode, logs, unnamed scratch notes, patch generator scripts, duplicated proposed-file bundles, and `.claude/`. `AGENTS.md` is the active instruction source.

The current checkout includes the full LangGraph/oracle pass-1 implementation surface and the local `TradingAgents/` reference checkout. The live Agintor package is authoritative.

## Product And Layer Map

- Factory: `agintor/factory/` turns a user goal or factory follow-up into runtime plans, benchmark plans, exported runtimes, and build summaries.
- Host: `agintor/runtime/host/` loads runtimes, performs preflight/validation, dispatches local or Docker execution, rewrites paths, and finalizes runtime responses.
- Runtime API: `agintor/runtime/api/` compiles user prompts or benchmark tasks into `ExecutionPlan` inputs and converts runtime protocol results back into solve/eval records.
- Runtime kernel: `agintor/runtime/kernel/` is the bundled solve-time implementation used by exported runtimes.
- LangGraph runtime path: `agintor/runtime/langgraph/` compiles validated `RuntimeSpec` payloads into a framework-backed runtime executor.
- Contracts: `agintor/contracts/` holds the Pydantic boundary objects shared across factory, host, runtime, evaluation, search, storage, and oracle code.
- Evaluation/search/oracle: `agintor/evaluation/`, `agintor/search/`, and `agintor/oracle/` provide benchmark suites, progress measurement, archive/search promotion, oracle package compilation, and validator-family routing.
- Storage/tracing/providers: `agintor/storage/`, `agintor/tracing/`, and `agintor/providers/` persist chats/runs/state/traces and route model-provider calls.
- Templates: `agintor/templates/` is what gets copied or packaged into built runtimes, plus prompt specifications for mutation/control/memory/tool workflows.
- Reference inputs: `TradingAgents/` is an external reference checkout used by the TradingAgents adapter/runtime-spec work; the authoritative Agintor implementation lives under `agintor/`.

## Relevant Documentation

- `Dev Docs/FASTEST_PATH_TO_MVP_MASTER_ACTION_PLAN.md` - active scope, sequencing, acceptance gates, and release boundary for the repair-factory MVP.
- `AGENTS.md` - active Codex/agent operating instructions for this repo.
- `Dev Docs/LANGGRAPH_ORACLE_PASS1_FINAL_PLAN.md` - implemented LangGraph/oracle pass-1 architecture reference.
- `Dev Docs/DEFERRED_ISSUES_LEDGER.md` - deferred issue ledger for real but non-blocking bugs.
- `Dev Docs/REPO_MAP.md` - this live-file orientation map.
- `Dev Docs/Collaboration Prompts/` - reusable Fable 5 and GPT-5.6 collaboration launchers.
- `Dev Docs/Archive Only - Zero Authority/README.md` - archive boundary and historical-content index.

## Active-Plan Live Files

The pass-1 LangGraph/oracle handoff proposed these files, and they are already represented in the live tree:

- `agintor/contracts/oracle.py`, `agintor/contracts/runtime_spec.py`, `agintor/contracts/spec_actions.py` - oracle, runtime-spec, and runtime-spec mutation contracts.
- `agintor/contracts/validation.py` - validation plans, claims, proof obligations, validator reports, evidence ledgers, comparison records, and related authority contracts.
- `agintor/oracle/` - oracle compiler, package IO, projections, QA, subagent scaffolding, registry, and validator families.
- `agintor/evaluation/oracle_runner.py` - sealed oracle evaluation runner.
- `agintor/evaluation/runners/repo_patch_runner.py` - controlled repo-patch fixture execution and evidence production.
- `agintor/runtime/langgraph/` - spec-backed runtime compiler, solve-time executor, operation service, entrypoint, adapters, state, and checkpoint stubs.
- `agintor/search/spec_mutator.py` - runtime-spec mutation path for search.
- `agintor/integrations/tradingagents/` - TradingAgents runtime-spec adapter and outcome validator integration.
- `agintor/templates/baseline_runtime_langgraph/` - baseline LangGraph runtime template files required by the final handoff.
- `agintor/runtime/sdk/defaults/runtime_profile.json` - tracked runtime profile fallback for clean-worktree bundling.

## Top-Level Files

- `.gitattributes` - repository line-ending and attribute settings.
- `.gitignore` - ignores local keys, generated runtimes, traces, caches, temp workspaces, and build artifacts.
- `AGENTS.md` - active Codex/agent operating instructions for this repo.
- `pyproject.toml` - package metadata, dependencies, optional extras, CLI entry point, package data, and pytest defaults.
- Historical implementation documents are consolidated under `Dev Docs/Archive Only - Zero Authority/`.

## Main Package: `agintor/`

### Root

- `agintor/__init__.py` - package marker and package-level docstring.
- `agintor/cli.py` - Typer CLI for init, solve, eval, evolve, oracle, inspect, and factory build commands.
- `agintor/utils.py` - shared hashing, JSON, math, randomness, and similarity helpers.

### Contracts

- `agintor/contracts/__init__.py` - contract re-export surface.
- `agintor/contracts/benchmarks.py` - benchmark tasks, verifier specs, suite evaluation records, and public/sealed benchmark projections.
- `agintor/contracts/branches.py` - branch budgets, branch plans, branch state/result snapshots, and queued-frame snapshots.
- `agintor/contracts/checkpoints.py` - checkpoint references, checkpoint envelope, and recovery failure kinds.
- `agintor/contracts/evidence.py` - evidence contracts, domain/authority enums, promotion decisions, optimizer updates, and decision field helpers.
- `agintor/contracts/execution.py` - execution-plan nodes, operation specs, input bindings, request file refs, and capability-scope normalization.
- `agintor/contracts/factory.py` - factory goal, criteria, benchmark plan, runtime plan, build/export summaries, and factory chat identity contracts.
- `agintor/contracts/oracle.py` - oracle package models: validation intents, claims, proof obligations, validators, task sets, and projections.
- `agintor/contracts/protocol.py` - solve/inspect/resume request and result protocol objects shared by host and runtime.
- `agintor/contracts/providers.py` - provider roles, provider plans, model requests/responses, and replay allocation contracts.
- `agintor/contracts/runtime.py` - runtime manifests, isolation policy, deployment contract, descriptors, run/attempt manifests, and runtime contract metadata.
- `agintor/contracts/runtime_spec.py` - spec-backed runtime schema for prompts, agents, graph nodes/edges, tools, models, and sealed/private key validation.
- `agintor/contracts/search.py` - archive entries, mutation/evaluation records, search stages, predictor observations, and evolution history rows.
- `agintor/contracts/sessions.py` - runtime session identity, runtime session seed, and runtime session message contracts.
- `agintor/contracts/side_effects.py` - side-effect receipts, terminalization helpers, and receipt reconciliation records.
- `agintor/contracts/spec_actions.py` - runtime-spec mutation actions, validation, application, result records, and mutation ledger entries.
- `agintor/contracts/state.py` - runtime state primitives: agents, children, tools, summaries, checkpoints, async handles, and memory nodes.
- `agintor/contracts/tracing.py` - provider-agnostic trace context and runtime event records.
- `agintor/contracts/verifiers.py` - shared benchmark verification, private verifier routing, checker execution, and private-result rescoring.

### Core

- `agintor/core/__init__.py` - core package marker.
- `agintor/core/exceptions.py` - shared domain exceptions for hard invalidation, branch cancellation, prompt adaptation, resume recovery, safety, patching, and runtime loading.
- `agintor/core/patches.py` - search/replace patch parsing, text application, file application, and patch construction helpers.
- `agintor/core/versioning.py` - single runtime contract version constant.

### Evaluation

- `agintor/evaluation/__init__.py` - evaluation package marker.
- `agintor/evaluation/benchmarks.py` - benchmark suite loading/registration plus demo and tool-frontier suite construction.
- `agintor/evaluation/challenge_generators.py` - generated tool-workflow challenge specs and difficulty modeling.
- `agintor/evaluation/evaluator.py` - `RuntimeEvaluator`; runs runtimes against suites and packages evaluation evidence.
- `agintor/evaluation/oracle_runner.py` - sealed oracle evaluation runner and payload preparation.
- `agintor/evaluation/pairwise_comparator.py` - artifact pairwise comparison and winner decoding.
- `agintor/evaluation/progress_oracle.py` - paired parent/child progress oracle, effect estimation, and progress config.
- `agintor/evaluation/scoring.py` - score calculation, reference-scale estimation, and mean-improvement helpers.

### Factory

- `agintor/factory/__init__.py` - factory package marker.
- `agintor/factory/export.py` - runtime export, runtime manifest/deployment contract writing, seed runtime creation, benchmark persistence, and export validation helpers.
- `agintor/factory/followups.py` - applies factory follow-up messages to existing factory chat/build projects.
- `agintor/factory/goals.py` - goal normalization, goal keyword/phrase/family extraction, success criteria, and goal amendments.
- `agintor/factory/pipeline.py` - build/evolution pipeline orchestration over goals, runtime plans, suites, export, and summaries.
- `agintor/factory/planning.py` - goal-conditioned benchmark-suite planning.
- `agintor/factory/prompt_builder.py` - mutation prompt construction.
- `agintor/factory/runtime_specs.py` - runtime-kind normalization and runtime-spec selection for spec-backed runtimes.
- `agintor/factory/service.py` - high-level factory service functions for new builds and follow-up builds.
- `agintor/factory/trace_context.py` - factory trace-context construction for build and follow-up operations.
- `agintor/factory/workspace.py` - build workspace layout object and path conventions.

### TradingAgents Integration

- `agintor/integrations/tradingagents/__init__.py` - TradingAgents integration package marker.
- `agintor/integrations/tradingagents/action_mapper.py` - maps TradingAgents recommendations into normalized order intents.
- `agintor/integrations/tradingagents/adapter.py` - seed spec and external TradingAgents config adaptation.
- `agintor/integrations/tradingagents/compiler.py` - goal-to-TradingAgents runtime-spec compilation.
- `agintor/integrations/tradingagents/data_snapshots.py` - market-data snapshot model and synthetic post-close snapshot writing.
- `agintor/integrations/tradingagents/ledgers.py` - trading decision ledger and ledger reconciliation.
- `agintor/integrations/tradingagents/outcome_oracle_family.py` - TradingAgents-specific oracle validator family.
- `agintor/integrations/tradingagents/spec.py` - TradingAgents spec constants and schema glue.
- `agintor/integrations/tradingagents/validators.py` - trading decision ledger validation.

### Learning

- `agintor/learning/__init__.py` - learning package marker.
- `agintor/learning/observations.py` - extracts predictor observations from search/evaluation/promotion records.
- `agintor/learning/predictors.py` - bootstrap regressors, ranking mixer, ensemble, model bank, and stable family seeding.

### Oracle

- `agintor/oracle/__init__.py` - oracle package marker.
- `agintor/oracle/compiler.py` - oracle package compiler and compiler config.
- `agintor/oracle/compiler_graph.py` - linear oracle compiler graph, compiler state, and compiler proposals.
- `agintor/oracle/package_io.py` - canonical oracle JSON, package hashing, package finalization, writing/loading, and lock checking.
- `agintor/oracle/projections.py` - public and sealed oracle projections.
- `agintor/oracle/qa.py` - oracle package QA runner and QA checks.
- `agintor/oracle/subagents.py` - oracle subagent proposal helpers and leakage critic scan.
- `agintor/oracle/validator_registry.py` - validator-family registry and default registry construction.

### Oracle Families

- `agintor/oracle/families/__init__.py` - default oracle family registry surface.
- `agintor/oracle/families/consent_proof.py` - consent/proof oracle family.
- `agintor/oracle/families/exact_private_answer.py` - exact private-answer oracle family.
- `agintor/oracle/families/factual_grounded.py` - factual groundedness oracle family.
- `agintor/oracle/families/human_audit.py` - human audit oracle family.
- `agintor/oracle/families/inspect_runner.py` - Inspect runner oracle family.
- `agintor/oracle/families/openai_eval_runner.py` - OpenAI eval runner oracle family.
- `agintor/oracle/families/pairwise_preference.py` - pairwise preference oracle family.
- `agintor/oracle/families/repo_patch.py` - repository patch oracle family.
- `agintor/oracle/families/schema_artifact.py` - schema/artifact oracle family.
- `agintor/oracle/families/stateful_service.py` - stateful service oracle family.
- `agintor/oracle/families/trace_state.py` - trace-state oracle family.
- `agintor/oracle/families/trading_outcome.py` - trading-outcome oracle family.

### Providers

- `agintor/providers/__init__.py` - provider package export surface.
- `agintor/providers/base.py` - provider base types, API-key loading, request options, usage/cost helpers, and deterministic local provider.
- `agintor/providers/env.py` - provider profile and environment variable naming helpers.
- `agintor/providers/failover.py` - failover provider wrapper.
- `agintor/providers/minimax.py` - MiniMax provider implementation through the Anthropic-compatible client.
- `agintor/providers/openai.py` - OpenAI provider implementation.
- `agintor/providers/payloads.py` - provider payload serialization and file-path rewriting.
- `agintor/providers/registry.py` - provider construction, payload loading, and provider cloning.
- `agintor/providers/replay.py` - replay provider for deterministic response playback.
- `agintor/providers/retry.py` - retry provider wrapper.

### Runtime API

- `agintor/runtime/__init__.py` - runtime package marker.
- `agintor/runtime/api/__init__.py` - runtime API export surface.
- `agintor/runtime/api/capabilities.py` - execution-plan provider/capability requirement checks.
- `agintor/runtime/api/context.py` - runtime policy context, prompt compilation, agent frame, budget, and runtime state.
- `agintor/runtime/api/failures.py` - runtime solve failure response construction.
- `agintor/runtime/api/plan_compiler.py` - converts benchmark tasks and user solve requests into execution plans.
- `agintor/runtime/api/plan_nodes.py` - plan node construction, capability intent hints, and request-file operations.
- `agintor/runtime/api/prompt_intent.py` - prompt intent heuristics for files, URLs, service actions, math, and repo patch signals.
- `agintor/runtime/api/protocol.py` - runtime request builders for inspect, task solve, user solve, batch, and blocked episodes.
- `agintor/runtime/api/request_loading.py` - solve request loading, benchmark-to-request conversion, and request file ref compilation.
- `agintor/runtime/api/results.py` - run-result to solve-result conversion and grouped run result reduction.
- `agintor/runtime/api/resume.py` - checkpoint-authoritative resume task/plan reconstruction and rebinding.
- `agintor/runtime/api/tracing.py` - trace identity helpers for benchmarks, runtime sessions, factory messages, and evaluation units.

### Runtime Host

- `agintor/runtime/host/__init__.py` - runtime host export surface.
- `agintor/runtime/host/backend_selection.py` - backend selection mixin.
- `agintor/runtime/host/finalization.py` - final response/result finalization mixin.
- `agintor/runtime/host/host.py` - `RuntimeHost`; central runtime dispatch facade.
- `agintor/runtime/host/local_process.py` - local runtime process execution mixin.
- `agintor/runtime/host/preflight.py` - host-side request/runtime preflight mixin.
- `agintor/runtime/host/resume_resolution.py` - resume target/checkpoint resolution mixin.
- `agintor/runtime/host/validation.py` - host-side runtime/request validation mixin.
- `agintor/runtime/host/backends/__init__.py` - host backend package marker.
- `agintor/runtime/host/backends/docker/__init__.py` - Docker backend package marker.
- `agintor/runtime/host/backends/docker/checkpoint_rewrite.py` - Docker checkpoint path rewrite mixin.
- `agintor/runtime/host/backends/docker/commands.py` - Docker command construction mixin.
- `agintor/runtime/host/backends/docker/entrypoint.py` - Docker runtime entrypoint.
- `agintor/runtime/host/backends/docker/executor.py` - Docker runtime executor.
- `agintor/runtime/host/backends/docker/image.py` - Docker image build/selection mixin.
- `agintor/runtime/host/backends/docker/path_mapping.py` - host/container path mapping mixin.
- `agintor/runtime/host/backends/docker/request_rewrite.py` - Docker request path rewrite mixin.
- `agintor/runtime/host/backends/docker/response_rewrite.py` - Docker response path rewrite mixin.
- `agintor/runtime/host/backends/docker/run_rewrite.py` - Docker run result/path rewrite mixin.

### Runtime Kernel

- `agintor/runtime/kernel/__init__.py` - kernel package export surface.
- `agintor/runtime/kernel/base.py` - `TaskRuntime`; composed runtime kernel class.
- `agintor/runtime/kernel/branching.py` - top-level branching mixin.
- `agintor/runtime/kernel/facade.py` - runtime kernel facade surface.
- `agintor/runtime/kernel/frames.py` - frame management mixin.
- `agintor/runtime/kernel/local_verifiers.py` - runtime-local benchmark verification helpers.
- `agintor/runtime/kernel/loop.py` - runtime execution loop mixin.
- `agintor/runtime/kernel/memory.py` - runtime memory mixin.
- `agintor/runtime/kernel/memory_graph.py` - short-term and long-term memory graph structures.
- `agintor/runtime/kernel/operations.py` - execution-plan operation mixin.
- `agintor/runtime/kernel/plan_helpers.py` - runtime execution-plan helper mixin.
- `agintor/runtime/kernel/predictors.py` - runtime predictor state.
- `agintor/runtime/kernel/progress.py` - progress/event recording mixin.
- `agintor/runtime/kernel/root_frame.py` - root frame execution mixin.
- `agintor/runtime/kernel/shell.py` - fixed shell, agent pool, message board, and open handle table.
- `agintor/runtime/kernel/side_effects.py` - side-effect receipt/ledger mixin.
- `agintor/runtime/kernel/tooling.py` - runtime tool invocation mixin.
- `agintor/runtime/kernel/verification.py` - runtime verification mixin.

### Runtime Kernel Branches

- `agintor/runtime/kernel/branches/__init__.py` - branch mixin export surface.
- `agintor/runtime/kernel/branches/budget.py` - branch budget mixin.
- `agintor/runtime/kernel/branches/execution.py` - branch execution mixin.
- `agintor/runtime/kernel/branches/providers.py` - branch provider cloning/allocation mixin.
- `agintor/runtime/kernel/branches/results.py` - branch result collection/reduction mixin.
- `agintor/runtime/kernel/branches/resume.py` - branch resume mixin.

### Runtime Kernel Checkpointing

- `agintor/runtime/kernel/checkpointing/__init__.py` - checkpointing mixin export surface.
- `agintor/runtime/kernel/checkpointing/publication.py` - checkpoint publication mixin.
- `agintor/runtime/kernel/checkpointing/recovery.py` - checkpoint recovery mixin.
- `agintor/runtime/kernel/checkpointing/restore.py` - checkpoint restore mixin.
- `agintor/runtime/kernel/checkpointing/results.py` - checkpoint result mixin.
- `agintor/runtime/kernel/checkpointing/snapshots.py` - checkpoint snapshot mixin.

### Runtime Kernel IO

- `agintor/runtime/kernel/io/__init__.py` - bounded IO mixin export surface.
- `agintor/runtime/kernel/io/paths.py` - bounded path handling mixin.
- `agintor/runtime/kernel/io/repo_patch.py` - repo patch IO mixin.
- `agintor/runtime/kernel/io/service_action.py` - service action IO mixin.

### Runtime LangGraph

- `agintor/runtime/langgraph/__init__.py` - LangGraph runtime package marker.
- `agintor/runtime/langgraph/adapters.py` - spec-backed policy objects, spec loading, artifact resolution, and spec task execution.
- `agintor/runtime/langgraph/checkpointing.py` - LangGraph state embedding/extraction helpers.
- `agintor/runtime/langgraph/compiler.py` - runtime-spec compiler.
- `agintor/runtime/langgraph/entrypoint.py` - LangGraph runtime solve entrypoint.
- `agintor/runtime/langgraph/executor.py` - compiled spec runtime, spec compilation, and code hash.
- `agintor/runtime/langgraph/operation_service.py` - operation service used by spec-backed LangGraph runtimes.
- `agintor/runtime/langgraph/state.py` - LangGraph runtime state and node result models.

### Runtime Loader, Project, Profile, SDK, Tools

- `agintor/runtime/loader.py` - runtime loading, identity hashing, Docker launch policy resolution, and loaded runtime descriptors.
- `agintor/runtime/profile.py` - runtime profile schema and default/control/execution/provider/tooling/memory/topology profile models.
- `agintor/runtime/project.py` - baseline runtime template lookup, runtime initialization, and demo suite writing.
- `agintor/runtime/prompts.py` - prompt spec loading and prompt instructions.
- `agintor/runtime/sdk/__init__.py` - runtime SDK export surface.
- `agintor/runtime/sdk/bundle.py` - kernel bundle manifest preview and runtime kernel bundling.
- `agintor/runtime/sdk/entrypoint.py` - bundled runtime SDK entrypoint.
- `agintor/runtime/sdk/defaults/runtime_profile.json` - default runtime profile copied into SDK/runtime contexts.
- `agintor/runtime/tools/__init__.py` - runtime tools package marker.
- `agintor/runtime/tools/execution.py` - tool execution mixin.
- `agintor/runtime/tools/executor.py` - tool executor.
- `agintor/runtime/tools/models.py` - registered tool model.
- `agintor/runtime/tools/registry.py` - tool registry.
- `agintor/runtime/tools/safety.py` - tool safety guard.
- `agintor/runtime/tools/sandbox.py` - tool sandbox manager.
- `agintor/runtime/tools/validation.py` - expression tool and candidate tool validation.

### Search

- `agintor/search/__init__.py` - search package marker.
- `agintor/search/archive.py` - quality-diversity archive, behavior descriptors, objective specs, and scope scheduling.
- `agintor/search/crossover.py` - method extraction and runtime crossover mutation.
- `agintor/search/engine.py` - evolution engine, promotion routing, promotion summary, and signal sufficiency output.
- `agintor/search/mutators.py` - heuristic and provider-driven source patch mutators.
- `agintor/search/spec_mutator.py` - heuristic/provider runtime-spec mutation actions and spec candidate IO.

### Storage

- `agintor/storage/__init__.py` - storage package marker.
- `agintor/storage/artifacts.py` - artifact mode/workspace origin handling, timestamped folder parsing, and artifact path resolution.
- `agintor/storage/factory_chat_store.py` - persistence for factory build/evolution chat projects.
- `agintor/storage/run_store.py` - run store and resume target resolution.
- `agintor/storage/runtime_session_store.py` - persistence for built-runtime chat sessions.

### Storage State Store

- `agintor/storage/state_store/__init__.py` - state store export surface.
- `agintor/storage/state_store/connection.py` - state DB connection and initialization mixin.
- `agintor/storage/state_store/indexers.py` - run/attempt/request/checkpoint/event/receipt/recovery indexers.
- `agintor/storage/state_store/layout.py` - state store path layout and layout errors.
- `agintor/storage/state_store/memory.py` - memory snapshot indexing and shard writing.
- `agintor/storage/state_store/queries.py` - checkpoint, artifact, branch lineage, recovery, and memory query helpers.
- `agintor/storage/state_store/rebuild.py` - dirty marking and canonical rebuild mixin.
- `agintor/storage/state_store/schema.py` - state store schema mixin.
- `agintor/storage/state_store/serializers.py` - state store serialization helpers.
- `agintor/storage/state_store/store.py` - composed `StateStore`.

### Templates

- `agintor/templates/baseline_runtime/control_policy.py` - baseline mutable control policy.
- `agintor/templates/baseline_runtime/deployment_contract.json` - baseline runtime deployment contract.
- `agintor/templates/baseline_runtime/memory_policy.py` - baseline mutable memory policy.
- `agintor/templates/baseline_runtime/runtime_manifest.json` - baseline runtime manifest.
- `agintor/templates/baseline_runtime/runtime_profile.json` - baseline runtime profile.
- `agintor/templates/baseline_runtime/tool_policy.py` - baseline mutable tool policy.
- `agintor/templates/baseline_runtime/topology_policy.py` - baseline mutable topology policy.
- `agintor/templates/baseline_runtime_langgraph/README.md` - baseline LangGraph runtime template notes.
- `agintor/templates/baseline_runtime_langgraph/langgraph_app.py` - baseline LangGraph app loader.
- `agintor/templates/baseline_runtime_langgraph/runtime_spec.json` - baseline spec-backed LangGraph runtime spec.
- `agintor/templates/prompts/control.local_check.json` - prompt spec for local control checks.
- `agintor/templates/prompts/control.repo_check.json` - prompt spec for repository-wide control checks.
- `agintor/templates/prompts/control.subtree_check.json` - prompt spec for subtree control checks.
- `agintor/templates/prompts/evolve.mutator_patch.json` - prompt spec for source patch mutation.
- `agintor/templates/prompts/memory.span_summarize.json` - prompt spec for memory span summaries.
- `agintor/templates/prompts/memory.unit_classify.json` - prompt spec for memory unit classification.
- `agintor/templates/prompts/runtime.child_checkpoint.json` - prompt spec for child runtime checkpoint summarization.
- `agintor/templates/prompts/tool.spec_generate.json` - prompt spec for tool spec generation.
- `agintor/templates/prompts/tool.spec_repair.json` - prompt spec for tool spec repair.

### Tracing

- `agintor/tracing/__init__.py` - tracing package export surface.
- `agintor/tracing/identity.py` - trace session IDs, trace grouping keys, runtime/factory/benchmark trace keys, and trace context resolution.
- `agintor/tracing/layout.py` - trace directory layout and human-readable trace view paths.
- `agintor/tracing/materialization.py` - trace materialization state loading and rebuild.
- `agintor/tracing/persistence.py` - provider trace persistence.
- `agintor/tracing/rendering.py` - trace subset rendering.

## Development Docs

- `Dev Docs/DEFERRED_ISSUES_LEDGER.md` - deferred issue ledger for real but non-blocking bugs.
- `Dev Docs/LANGGRAPH_ORACLE_PASS1_FINAL_PLAN.md` - implemented pass-1 architecture reference.
- `Dev Docs/REPO_MAP.md` - live-file orientation map.
- `Dev Docs/Collaboration Prompts/FABLE5_COLLAB_SYSTEM_PROMPT.md` - Fable 5 collaboration launcher.
- `Dev Docs/Collaboration Prompts/GPT56_COLLAB_SYSTEM_PROMPT.md` - GPT-5.6 collaboration launcher.
- `Dev Docs/Archive Only - Zero Authority/README.md` - historical archive index.

## TradingAgents Reference Checkout

`TradingAgents/` is a local third-party/reference runtime checkout used to ground the Agintor TradingAgents adapter and spec-backed runtime work. This section indexes source/configuration paths that matter for adapter/runtime work and skips tests, screenshots/assets, lockfiles, static banners, ignore files, and upstream TODO noise.

### TradingAgents Project Files

- `TradingAgents/.env.enterprise.example` - enterprise environment example.
- `TradingAgents/.env.example` - default environment example.
- `TradingAgents/Dockerfile` - TradingAgents container build file.
- `TradingAgents/README.md` - TradingAgents project readme.
- `TradingAgents/CHANGELOG.md` - upstream release history for the local reference version.
- `TradingAgents/docker-compose.yml` - TradingAgents compose setup.
- `TradingAgents/main.py` - TradingAgents root launcher.
- `TradingAgents/pyproject.toml` - TradingAgents Python package metadata.
- `TradingAgents/requirements.txt` - TradingAgents dependency list.
- `TradingAgents/scripts/smoke_structured_output.py` - structured output smoke script.

### TradingAgents CLI

- `TradingAgents/cli/__init__.py` - TradingAgents CLI package marker.
- `TradingAgents/cli/announcements.py` - CLI announcement UI.
- `TradingAgents/cli/config.py` - CLI configuration.
- `TradingAgents/cli/main.py` - TradingAgents CLI entrypoint.
- `TradingAgents/cli/models.py` - CLI model/config types.
- `TradingAgents/cli/stats_handler.py` - CLI stats display.
- `TradingAgents/cli/utils.py` - CLI utility helpers.

### TradingAgents Core Package

- `TradingAgents/tradingagents/__init__.py` - TradingAgents package marker.
- `TradingAgents/tradingagents/default_config.py` - default TradingAgents configuration.

### TradingAgents Agents

- `TradingAgents/tradingagents/agents/__init__.py` - agents package marker.
- `TradingAgents/tradingagents/agents/schemas.py` - agent schema definitions.
- `TradingAgents/tradingagents/agents/analysts/fundamentals_analyst.py` - fundamentals analyst agent.
- `TradingAgents/tradingagents/agents/analysts/market_analyst.py` - market analyst agent.
- `TradingAgents/tradingagents/agents/analysts/news_analyst.py` - news analyst agent.
- `TradingAgents/tradingagents/agents/analysts/sentiment_analyst.py` - sentiment analyst agent.
- `TradingAgents/tradingagents/agents/analysts/social_media_analyst.py` - social media analyst agent.
- `TradingAgents/tradingagents/agents/managers/portfolio_manager.py` - portfolio manager agent.
- `TradingAgents/tradingagents/agents/managers/research_manager.py` - research manager agent.
- `TradingAgents/tradingagents/agents/researchers/bear_researcher.py` - bear researcher agent.
- `TradingAgents/tradingagents/agents/researchers/bull_researcher.py` - bull researcher agent.
- `TradingAgents/tradingagents/agents/risk_mgmt/aggressive_debator.py` - aggressive risk debater.
- `TradingAgents/tradingagents/agents/risk_mgmt/conservative_debator.py` - conservative risk debater.
- `TradingAgents/tradingagents/agents/risk_mgmt/neutral_debator.py` - neutral risk debater.
- `TradingAgents/tradingagents/agents/trader/trader.py` - trader agent.

### TradingAgents Agent Utilities

- `TradingAgents/tradingagents/agents/utils/agent_states.py` - agent state definitions.
- `TradingAgents/tradingagents/agents/utils/agent_utils.py` - shared agent utilities.
- `TradingAgents/tradingagents/agents/utils/core_stock_tools.py` - core stock tool helpers.
- `TradingAgents/tradingagents/agents/utils/fundamental_data_tools.py` - fundamental-data tool helpers.
- `TradingAgents/tradingagents/agents/utils/memory.py` - memory utilities.
- `TradingAgents/tradingagents/agents/utils/news_data_tools.py` - news data tool helpers.
- `TradingAgents/tradingagents/agents/utils/rating.py` - rating helpers.
- `TradingAgents/tradingagents/agents/utils/structured.py` - structured output helpers.
- `TradingAgents/tradingagents/agents/utils/technical_indicators_tools.py` - technical-indicator tool helpers.

### TradingAgents Dataflows

- `TradingAgents/tradingagents/dataflows/__init__.py` - dataflows package marker.
- `TradingAgents/tradingagents/dataflows/alpha_vantage.py` - Alpha Vantage aggregate dataflow.
- `TradingAgents/tradingagents/dataflows/alpha_vantage_common.py` - shared Alpha Vantage helpers.
- `TradingAgents/tradingagents/dataflows/alpha_vantage_fundamentals.py` - Alpha Vantage fundamentals dataflow.
- `TradingAgents/tradingagents/dataflows/alpha_vantage_indicator.py` - Alpha Vantage indicator dataflow.
- `TradingAgents/tradingagents/dataflows/alpha_vantage_news.py` - Alpha Vantage news dataflow.
- `TradingAgents/tradingagents/dataflows/alpha_vantage_stock.py` - Alpha Vantage stock dataflow.
- `TradingAgents/tradingagents/dataflows/config.py` - dataflow configuration.
- `TradingAgents/tradingagents/dataflows/interface.py` - dataflow interface.
- `TradingAgents/tradingagents/dataflows/reddit.py` - Reddit dataflow.
- `TradingAgents/tradingagents/dataflows/stockstats_utils.py` - stockstats utilities.
- `TradingAgents/tradingagents/dataflows/stocktwits.py` - StockTwits dataflow.
- `TradingAgents/tradingagents/dataflows/utils.py` - dataflow utilities.
- `TradingAgents/tradingagents/dataflows/y_finance.py` - yfinance dataflow.
- `TradingAgents/tradingagents/dataflows/yfinance_news.py` - yfinance news dataflow.

### TradingAgents Graph

- `TradingAgents/tradingagents/graph/__init__.py` - graph package marker.
- `TradingAgents/tradingagents/graph/checkpointer.py` - graph checkpointer.
- `TradingAgents/tradingagents/graph/conditional_logic.py` - graph conditional edge logic.
- `TradingAgents/tradingagents/graph/propagation.py` - graph propagation flow.
- `TradingAgents/tradingagents/graph/reflection.py` - reflection flow.
- `TradingAgents/tradingagents/graph/setup.py` - graph setup.
- `TradingAgents/tradingagents/graph/signal_processing.py` - signal processing.
- `TradingAgents/tradingagents/graph/trading_graph.py` - central TradingAgents graph.

### TradingAgents LLM Clients

- `TradingAgents/tradingagents/llm_clients/__init__.py` - LLM client package marker.
- `TradingAgents/tradingagents/llm_clients/anthropic_client.py` - Anthropic client.
- `TradingAgents/tradingagents/llm_clients/api_key_env.py` - API key environment handling.
- `TradingAgents/tradingagents/llm_clients/azure_client.py` - Azure OpenAI client.
- `TradingAgents/tradingagents/llm_clients/base_client.py` - base LLM client.
- `TradingAgents/tradingagents/llm_clients/capabilities.py` - client capability descriptors.
- `TradingAgents/tradingagents/llm_clients/factory.py` - LLM client factory.
- `TradingAgents/tradingagents/llm_clients/google_client.py` - Google client.
- `TradingAgents/tradingagents/llm_clients/model_catalog.py` - model catalog.
- `TradingAgents/tradingagents/llm_clients/openai_client.py` - OpenAI client.
- `TradingAgents/tradingagents/llm_clients/validators.py` - client/model validators.
