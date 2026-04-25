# Agintor Environment and Complexity Cleanup Plan

## Summary
- Standardize the repo on a real local `.venv` using Python 3.12 and Pydantic v2.
- Remove dual-version compatibility code, legacy schema labels, and disposable checkpoint/runtime migration gates.
- Keep only one lightweight runtime contract check so Docker host/runtime launches can detect the wrong bundled code.
- Simplify factory/export artifacts by removing meta-commentary records that do not affect execution.

## Findings
- There is no regular project venv: no `.venv`, `.python-version`, lockfile, `requirements*.txt`, or README, even though `pyproject.toml` points at `README.md`.
- The active `python` is `C:\Users\yaros\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe`, with Pydantic `2.13.2`; the repo declares `pydantic>=1.10,<2`.
- `python -m pytest ...` currently fails during collection on Pydantic v1 validators under Pydantic v2. Typer, pytest, OpenAI, Anthropic, NumPy, and PyYAML are otherwise within declared ranges.
- `pip show agintor` is absent in the active env, while `agintor.exe` on PATH comes from a separate global Python install. That can make CLI behavior differ from `python -m pytest`.
- Version clutter inventory found 34 schema/runtime version string sites and 37 `.v1` prompt/schema id sites, including runtime ABI v5, storage schema v3, checkpoint envelope v4, execution plan v1, build summary v2, prompt IDs, and factory artifact schema labels.
- The main over-complexity clusters are `schemas.py`, `runtime_api.py`, `runtime_builder.py`, `runtime_host.py`, and `state_store.py`, plus the 12-mixin `TaskRuntime` lattice.

## Key Changes
- Add local environment setup:
  - Add `.venv/` to `.gitignore`.
  - Add `.python-version` with `3.12`.
  - Add a minimal `README.md` so packaging metadata is valid.
  - Update docs to use `py -3.12 -m venv .venv`, `.venv\Scripts\python -m pip install -e ".[dev,hosted]"`, and `.venv\Scripts\python -m pytest`.

- Migrate to Pydantic v2:
  - Change `pyproject.toml` to `pydantic>=2,<3`.
  - Replace v1 `@validator`, `@root_validator`, and `class Config` with v2 validators/config.
  - Delete `agintor/pydantic_compat.py` and replace wrapper calls with direct `.model_dump()`, `.model_copy()`, and `Model.model_validate()`.
  - Remove tests that exist only to prove v1/v2 compatibility.

- Collapse versioning:
  - Replace `RUNTIME_ABI_VERSION`, `KERNEL_VERSION`, `STORAGE_SCHEMA_VERSION`, `CHECKPOINT_ENVELOPE_SCHEMA_VERSION`, and `STATE_STORE_SCHEMA_VERSION` with one `RUNTIME_CONTRACT_VERSION = __version__`.
  - Keep that single value only in runtime inspect/capability exchange and Docker host/runtime validation.
  - Remove checkpoint/storage migration logic and fail-closed old-envelope tests; all existing runs, checkpoints, and exported runtimes are disposable.
  - Rename prompt templates from `*.v1.json` to stable names like `memory.span_summarize.json`, and update runtime profile defaults.
  - Remove `schema_version` fields from artifact metadata, kernel manifests, execution plans, trace materialization state, benchmark JSON, and checkpoint envelopes unless needed by a third-party format.

- Simplify factory/export artifacts:
  - Remove persisted `assumption_register.json`, `planning_diagnostics.json`, `replan_contract.json`, `runtime_provenance_bundle.json`, and `export_validation.json`.
  - Keep validation as in-memory checks that raise clear errors, not as verbose “certified/uncertified” receipt artifacts.
  - Delete `ArtifactMetadata` and `artifact_metadata` fields from internal models.
  - Keep only the useful build outputs: goal spec, success criteria, benchmark plan, verifier bundle, runtime plan, deployment contract, export summary, and build summary.

- Reduce AI-shaped complexity:
  - Replace “compatibility/result/recoverability” terminology with plain lifecycle states and errors where possible.
  - Remove `exact_compatible`, `degraded_compatible`, `fail_closed`, `best_effort` recovery ceremony for disposable checkpoints.
  - Centralize capability checks so `capability_intent` is not repeatedly copied through plan metadata unless a runtime decision actually reads it.
  - Split oversized modules only after version/artifact cleanup, starting with `schemas.py` into factory schemas, runtime protocol schemas, and durability schemas.

## Test Plan
- Recreate the local environment from scratch with `.venv`.
- Run `python -m pip check`.
- Run `python -m pytest` with the default offline suite.
- Run focused Docker checks: container runtime tests and one Docker solve that mounts and writes a request file.
- Run CLI smoke tests through the installed `.venv` script: `agintor init-runtime`, `agintor solve --prompt`, and `agintor eval --suite demo --seeds 0`.
- Verify `rg "pydantic_compat|root_validator|allow_reuse|agintor-runtime-abi|storage-v|checkpoint-envelope.v|\\.v1"` returns no unintended compatibility leftovers.

## Assumptions
- Existing checkpoints, runs, exported runtimes, and `.tmp*` artifacts may be invalidated or deleted.
- No legacy Pydantic v1 support is required.
- One runtime contract value is enough for Docker sanity checking.
- Factory/export artifacts are internal MVP files, not public APIs.
