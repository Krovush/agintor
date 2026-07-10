# Spec-Backed LangGraph Runtime Refactor

## Summary
Implement a parallel v2 runtime path where Agintor still owns factory/evaluation/search, but the runtimes it builds and evolves are readable `RuntimeSpec` artifacts compiled to LangGraph. New runtimes use v2 by default; legacy policy-file runtimes remain loadable only as a transition path. The first UX priority is an evidence-linked mutation ledger, not a visual UI.

## Key Changes
- Add LangGraph as a core dependency, targeting its standalone graph/persistence APIs rather than adding broad LangChain dependency.
- Add a typed `RuntimeSpec` contract bundled with exported runtimes:
  - `agents`: id, role, instructions, model policy tag, allowed tools, scope tags.
  - `edges`: source, target, condition, fan-in/fan-out metadata.
  - `tools`: allowed categories and routing preferences.
  - `memory`: short/long-term policy knobs.
  - `execution`: budget, verifier, stop/checkpoint policy.
  - `mutation_history`: applied action ids and parent spec digest.
- Extend `RuntimeManifest` with `runtime_kind: "policy_v1" | "langgraph_spec_v2"` and `runtime_spec_file`.
- Add a new v2 template containing `runtime_spec.json`, deployment contract, runtime profile, and no mutator-visible Python policy files.
- Update `init-runtime` and `build-runtime` so newly created runtimes default to v2, while existing v1 test/runtime paths remain supported during migration.
- Keep one `RUNTIME_CONTRACT_VERSION`; do not add checkpoint/runtime ABI version axes.

## Runtime Execution
- Add a LangGraph-backed executor selected by `runtime_kind == "langgraph_spec_v2"`.
- Keep `ExecutionPlan` as the canonical compiler output for benchmark and prompt solves.
- Compile each `ExecutionPlan` into a LangGraph `StateGraph`:
  - graph state stores plan values, terminal outputs, budget usage, verification status, trace ids, side-effect receipts, and runtime session carryover.
  - each `PlanNode` becomes a LangGraph node or grouped subgraph step.
  - branch groups map to parallel graph nodes followed by existing merge semantics.
- Extract current operation behavior into a shared runtime operation service so LangGraph nodes reuse existing tool/builtin/repo/service/verify semantics instead of reimplementing them.
- Preserve `RuntimeHost`, `RunStore`, Docker/local backend preflight, path rewriting, private benchmark projection, and exported runtime entrypoint behavior.
- Store LangGraph state snapshots inside existing checkpoint artifacts, and keep existing `CheckpointEnvelope` as the host-facing resume contract.

## Evolution And Mutation
- Add `SpecActionMutator` for v2 runtimes.
- Replace SEARCH/REPLACE patch mutation with an Action DSL:
  - `add_agent`, `remove_agent`, `update_agent`, `set_edge`, `remove_edge`, `set_tool_policy`, `set_memory_policy`, `set_budget_policy`, `set_verifier_policy`, `set_routing_weight`.
  - every action requires `scope`, `rationale`, `expected_effect`, and bounded target ids.
- Stage 0 for v2 applies actions to `runtime_spec.json`, validates the spec, writes the child runtime, and records a `mutation_action_id`.
- Keep scheduler scopes as `top/mem/tool/ctl`, but map each action type to one or more scopes.
- Update runtime identity and archive complexity metrics to hash/count the normalized spec rather than mutable Python policy LOC.
- Keep `ProgressOracle`, evidence contracts, private expected answers, promotion routing, archives, predictors, and export candidate selection custom.

## Readability / UX
- Add required v1 readability outputs:
  - `mutation_ledger.jsonl`: parent hash, child hash, action ids, touched scopes, human explanation, spec diff summary.
  - Stage 4 promotion ledger links each decision to the mutation action ids that produced the child.
  - build/evolve history rows include `spec_digest`, `parent_spec_digest`, `mutation_action_ids`, `improved_axes`, `regressed_axes`, and promotion type.
- Add CLI surfaces:
  - `agintor inspect-runtime <dir>` prints runtime kind, agents, tools, topology, memory, budgets, spec digest, and latest evidence summary.
  - `agintor diff-runtime <parent> <child>` prints applied actions, graph/topology changes, changed prompts/tools/memory, and linked eval outcome.
- Do not build a visual graph UI in v1; optionally emit Mermaid text from `inspect-runtime` if cheap.

## Test Plan
- Unit tests for `RuntimeSpec` validation, stable normalization, spec digest, and Action DSL application/rejection.
- Loader tests proving v2 runtimes load, hash changes when spec changes, and factory-only files do not enter exported runtime bundles.
- Runtime tests proving v2 solves the deterministic prompt-mode validation task through `RuntimeHost` with zero model calls.
- Evaluation tests proving v2 candidates pass Stage 0 via spec actions, not Python patches, and Stage 4 ledgers link promotion decisions to mutation action ids.
- Evolution tests proving `evolve` can accept a v2 child, archive it, select it as future parent, and export it.
- Session tests proving prompt solve carryover still separates factory chat, runtime chat, and benchmark grouping.
- Export tests proving `build-runtime` produces a self-contained v2 runtime that loads and solves after export.
- Docker/path tests proving v2 preserves current backend preflight and response rewriting.
- Validation commands: `python -m compileall -q agintor`, focused runtime/evaluator/search/factory tests, then `git diff --check`.

## Assumptions
- New runtimes default to v2 because MVP exported runtimes/checkpoints are disposable under AGENTS.md.
- Legacy v1 remains only long enough to keep transition tests meaningful; it is not a long-term compatibility promise.
- LangGraph is used for runtime graph execution and checkpointable state, based on its Graph API, persistence, and functional API docs.
- OpenAI Agents SDK, LangSmith, Pydantic AI, and DSPy are not part of this first implementation; they remain possible later integrations for tracing, eval dashboards, durable backends, or optimizer improvements.
