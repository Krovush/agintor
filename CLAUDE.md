## Project

Agintor CLI MVP — "bounded evolutionary runtime search for agent topology, memory, tooling, and control". Python 3.12, Pydantic v2 (`>=2,<3`), Typer CLI.

## Common commands

Install in editable mode with the test extras:

```bash
py -3.12 -m venv .venv
.\.venv\Scripts\python -m pip install -e ".[dev]"             # core + pytest
.\.venv\Scripts\python -m pip install -e ".[dev,openai]"      # add OpenAI provider
.\.venv\Scripts\python -m pip install -e ".[dev,minimax]"     # add MiniMax (anthropic SDK) provider
.\.venv\Scripts\python -m pip install -e ".[dev,hosted]"      # both hosted providers
```

Run the default (offline/fast) test suite — `pytest.ini_options.addopts` already excludes `heavy`, `docker`, and `live_openai` markers:

```bash
.\.venv\Scripts\python -m pytest                                           # whole suite (offline, fast markers)
.\.venv\Scripts\python -m pytest tests/test_runtime_host.py                # single file
.\.venv\Scripts\python -m pytest tests/test_runtime_host.py::test_name     # single test
.\.venv\Scripts\python -m pytest -m integration                            # runtime-backed offline integration
.\.venv\Scripts\python -m pytest -m heavy                                  # runtime-heavy coverage (opt in)
.\.venv\Scripts\python -m pytest -m docker                                 # docker-backed (opt in)
.\.venv\Scripts\python -m pytest -m live_openai                            # live OpenAI (opt in, needs key)
```

CLI (installed as `agintor`, defined in [agintor/cli.py](agintor/cli.py)):

```bash
agintor init-runtime <dest>                                      # materialize a baseline runtime dir
agintor solve <runtime_dir> <task_id> --suite demo               # benchmark-mode solve
agintor solve <runtime_dir> --prompt "..."                       # user-request mode
agintor eval <runtime_dir> --suite demo --seeds 0,1,2            # multi-seed evaluation
agintor evolve <runtime_dir> --steps 10 --mutator heuristic      # bounded search
agintor build-runtime "<goal>" --destination <dir> --steps 10    # full factory pipeline
```

Relevant CLI flags: `--provider {local,openai,minimax}`, `--runtime-backend {local,docker}`, `--api-key-file`, `--profile`, `--artifact-mode`, `--workspace`.

## Big-picture architecture

The repo enforces a four-layer split: **factory → host → runtime kernel → policy**. This split is load-bearing — do not collapse it.

### Layers

- **Factory** ([agintor/runtime_builder.py](agintor/runtime_builder.py), [goal_rubric.py](agintor/goal_rubric.py), [evolution.py](agintor/evolution.py), [evaluator.py](agintor/evaluator.py), [mutator.py](agintor/mutator.py), [archive.py](agintor/archive.py)): the `build-runtime` pipeline. Freezes planning artifacts (`goal_spec.json`, `success_criteria.json`, `benchmark_plan.json`, `verifier_bundle.json`, `runtime_plan.json`, `deployment_contract.json`, `export_summary.json`, `build_summary.json`) in the build workspace, runs bounded evolution, validates a leader, and exports a self-describing runtime. Factory-only knobs must not affect exported runtime hash.
- **Host** ([agintor/runtime_host.py](agintor/runtime_host.py), [agintor/container_runtime.py](agintor/container_runtime.py)): launcher + protocol client. Owns CLI parsing, benchmark lookup, prompt-file loading, and envelope construction. The host does *not* implement solve semantics; it speaks the runtime protocol over stdio (local) or mounted request/result files (docker).
- **Runtime kernel** ([agintor/runtime_sdk/](agintor/runtime_sdk/), [agintor/task_runtime/](agintor/task_runtime/)): the bundled solve-time implementation. Every exported runtime vendors `runtime_sdk/` so runtimes are host-independent. `runtime_sdk/runtime_entry.py` is the protocol entrypoint; actual execution lives in the `TaskRuntime` mixin stack.
- **Policy** ([agintor/templates/baseline_runtime/](agintor/templates/baseline_runtime/)): four mutable Python files that the mutator can patch — `topology_policy.py`, `memory_policy.py`, `tool_policy.py`, `control_policy.py`. The mutator-visible control surface is narrowed to `assign_model`, `request_checks`, `stop_policy`. Everything else is fixed by the kernel.

### TaskRuntime is a mixin composition

[agintor/runner.py](agintor/runner.py) is just the public façade — it re-exports `TaskRuntime` from [agintor/task_runtime/base.py](agintor/task_runtime/base.py). The real class is built by multiple inheritance over ~12 mixins in [agintor/task_runtime/](agintor/task_runtime/): `ExecutionLoopMixin`, `CheckpointingMixin`, `SideEffectsMixin`, `BranchExecutionMixin`, `BranchingMixin`, `PlanHelpersMixin`, `FramesMixin`, `MemoryMixin`, `OperationsMixin`, `ToolingMixin`, `BoundedIOMixin`, `VerificationMixin`. When touching runtime behavior, find the mixin that owns the concern rather than editing `base.py`.

Downstream code must treat `agintor/task_runtime/` as **bundled kernel internals**; factory/evaluator code must not reach into it — it should go through the runtime entrypoint or [runtime_api.py](agintor/runtime_api.py).

### Runtime contract version

The MVP uses one lightweight `RUNTIME_CONTRACT_VERSION`, derived from the package version, to catch host/runtime bundle mismatches. Checkpoints and exported runtimes are disposable during active development, so do not add cross-version upgrade support or separate ABI/kernel/storage version axes.

### Execution plan is the single contract

Both benchmark tasks and user-request prompts compile down to one `ExecutionPlan` ([agintor/runtime_api.py](agintor/runtime_api.py)) before execution. Benchmark request identity is normalized as `benchmark.<task_id>.seed_<seed>`. `OpenAITraceContext` ([agintor/openai_trace.py](agintor/openai_trace.py)) is provider-agnostic correlation metadata — despite the name — and is propagated through request, plan, policy context, frame, and branch objects.

### Fixed shell + policies

[agintor/shell.py](agintor/shell.py) owns the solve-time substrate: message board, handles, memory (short-term / long-term graphs), tools, predictors. Policies read and react; they do not mutate the shell directly outside the defined surface. Checkpoints snapshot runtime-owned state ([agintor/run_store.py](agintor/run_store.py)); branch isolation is copy-in / publication-out.

### Runtime backends

`--runtime-backend local` runs the kernel in-process; `--runtime-backend docker` executes via [agintor/container_runtime.py](agintor/container_runtime.py). The protocol contract is identical across backends — local uses stdio, docker uses mounted request/result files.

## Workstream documents

[implementation_workstreams/](implementation_workstreams/) contains the authoritative scope and boundary documents (WS1–WS5). When a change could plausibly cross a boundary, re-read the relevant workstream before acting — each WS lists what it owns, what it does *not* own, and which decisions are frozen.

- **WS1** — factory build & export contract (frozen first; do not redesign)
- **WS2** — runtime execution, orchestration, isolation
- **WS3** — state, memory, durability (RunStore, state_store, trace topology)
- **WS4** — benchmarks, evaluation, search (consumes WS3 durability)
- **WS5** — tooling, providers, control (consumes WS4 evidence)

[POST_WS5_DEBUGGING_LEDGER.MD](POST_WS5_DEBUGGING_LEDGER.MD) lists known deferred issues with the contract boundary they sit on — consult it before "fixing" checkpoint-resume, replay-clone, or host-finalization behavior, as several apparent bugs are intentionally deferred.

- When you identify an issue that does not pertain to your current workstream, append `POST_WS5_DEBUGGING_LEDGER.MD` with the issue. Notify the user instead when you spot an issue in your workstream scope

## Provider & secrets layout

Provider selection goes through [agintor/providers.py](agintor/providers.py) (`build_provider`), with adapter files `provider_openai.py`, `provider_minimax.py`, `provider_common.py`. Keys can be supplied via `--api-key-file`; local files `OpenAI API Key.txt` and `MiniMax API Key.txt` are `.gitignore`'d and must not be committed. `hosted` extras pull in the upstream SDKs.

## Generated / ignored directories

`.agintor_runs/`, `.agintor_evo/`, `tests/_artifacts/`, `openai_api_traces/`, `agintor/templates/baseline_runtime/` (regenerated during packaging), and anything under `.tmp*`/`.temp*`/`_codex_tmp_*` are gitignored workspaces — safe to delete, not to edit for persistent behavior.

# Rules:
- DO NOT implement hotfixes, patches, demos, fallbacks, or any other kind of temporary, ineffectual solutions. Implementations must be proper, follow best practices, and be production-ready. If a problem is architectural in nature, you MUST refactor said architecture instead of patching it with slopy code that barely works and will cause many problems down the line.
- Your biggest recurring weakness is theory-of-mind failure in agent-to-agent communication: you overfit outputs to your own context, leak planning history and hidden assumptions, and include information that feels useful from your perspective but is unnecessary or confusing for the recipient. When writing plans, prompts, handoffs, summaries, or instructions for another agent or implementer, optimize strictly for their context and information boundary. Output only what they need to act correctly; omit meta-commentary, prior-draft history, and any detail that is relevant only from your own perspective.
- I DO NOT CARE about backward compatibility preservation. Agintor is very far from production, preserving legacy runtime compatibility is unnecessary.
- Record non-urgent issues/bugs you find to `C:\Users\yaros\Desktop\Agintor MVP\POST_WS5_DEBUGGING_LEDGER.MD`. If an issue is not critical for WS3 completion, and WS4 readiness, and is better addressed later in the final massive debugging round, then record it here and move on.

## Note

- If you are reading this, DO NOT read AGENTS.md, it is identical to this file, don't waste tokens
