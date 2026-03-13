# Runtime Boundary

## Purpose

This repository now treats Agintor itself and the MAS runtimes it exports as two different execution environments.

- Agintor is the control plane.
- Exported MAS runtimes are the runtime artifacts Agintor builds and ships.

They no longer share the same default live-provider contract.

## What Changed

### 1. Provider layer was generalized

`agintor/providers.py` now has a generic hosted-provider abstraction instead of a single OpenAI-only live path.

- `OpenAIProvider` remains available for Agintor control-plane use.
- `MiniMaxProvider` was added for exported MAS runtimes.
- `build_provider()` now supports `local`, `openai`, and `minimax`.
- The non-heuristic mutator path is now provider-backed rather than OpenAI-branded.

### 2. Runtime artifacts now carry their own provider contract

`agintor/runtime_profile.py` now stores runtime-facing provider settings under `runtime_provider`.

That profile is embedded into exported runtimes and includes:

- provider name
- base URL
- API key env name
- API key file env name
- model map
- pricing env name
- temperature

Legacy profile overrides that still use the old `provider` key are migrated into `runtime_provider` during load.

### 3. Exported MAS runtimes now default to MiniMax

`agintor/templates/baseline_runtime/runtime_profile.json` now sets the exported runtime default to MiniMax:

- provider: `minimax`
- base URL: `https://api.minimax.io/v1`
- model map: `MiniMax-M2.5` for `small`, `medium`, and `large`

This means newly initialized and newly built runtime artifacts carry MiniMax as their own runtime default instead of inheriting Agintor's OpenAI control-plane defaults.

### 4. Agintor control-plane and MAS runtime provider surfaces were split

`agintor/cli.py` now separates the meaning of provider selection by command:

- `solve` and `eval`
  - default to the runtime artifact's embedded `runtime_provider`
  - still allow `--provider local` for deterministic offline checks
- `evolve` and `build-runtime`
  - use the Agintor-side provider selected on the CLI
  - do not inherit the exported runtime's embedded provider contract

`build-runtime` also now forwards `--mutator` all the way into `EvolutionEngine`.

### 5. Container execution respects the same split

`agintor/container_entry.py` and `agintor/container_runtime.py` were updated so containerized runtime execution no longer assumes OpenAI-only settings.

- Hosted dependency install is now via `.[hosted]`
- MiniMax runtime env vars are forwarded into Docker
- provider key mounts are generic instead of OpenAI-named
- runtime provider config is only applied when the selected runtime provider matches the embedded runtime profile

## Exact Isolation Mechanism

The two environments are isolated by configuration namespace, code path, and artifact boundary.

### Agintor control plane

Agintor uses its own provider surface for outer-loop work such as evolution and goal-conditioned building.

Current OpenAI-side env namespace:

- `OPENAI_API_KEY`
- `OPENAI_BASE_URL`
- `AGINTOR_OPENAI_KEY_FILE`
- `AGINTOR_OPENAI_SMALL_MODEL`
- `AGINTOR_OPENAI_MEDIUM_MODEL`
- `AGINTOR_OPENAI_LARGE_MODEL`
- `AGINTOR_OPENAI_PRICING`

### Exported MAS runtime

Exported runtime artifacts use their own embedded runtime-provider contract and MiniMax namespace.

Current MiniMax runtime env namespace:

- `AGINTOR_MAS_MINIMAX_API_KEY`
- `AGINTOR_MAS_MINIMAX_KEY_FILE`
- `AGINTOR_MAS_MINIMAX_BASE_URL`
- `AGINTOR_MAS_MINIMAX_SMALL_MODEL`
- `AGINTOR_MAS_MINIMAX_MEDIUM_MODEL`
- `AGINTOR_MAS_MINIMAX_LARGE_MODEL`
- `AGINTOR_MAS_MINIMAX_PRICING`

### Why this is a real separation

The runtime artifact now carries its own `runtime_profile.json`, and `solve` or `eval` default to that runtime-facing provider contract.

Agintor's own build and evolution commands still use the explicitly selected Agintor-side provider. The outer loop does not silently inherit the MAS runtime's MiniMax provider settings.

## Secret Handling

The repo now stops treating operator key files as part of tracked project state.

- `OpenAI API Key.txt` is removed from the git index but left on disk locally.
- `OpenAI API Key.txt` and `MiniMax API Key.txt` are both ignored by `.gitignore`.

This keeps local operator credentials out of the repo state while preserving local files for personal use.

History rewriting was not executed here because `git filter-repo` is not installed in this environment.

## Verification Performed

Checked with:

- targeted `py_compile`
- `pytest tests/test_cli.py`
- `pytest tests/test_runtime_builder.py`
- `pytest tests/test_runtime_identity.py`
- focused provider and control-policy assertions in `tests/test_core.py`
- `pytest tests/test_evolution.py -k runs_smoke`
- end-to-end `python -m agintor.cli build-runtime ... --provider local --mutator heuristic --runtime-backend local --steps 1`

The build smoke output reported:

- `agintor_provider = local`
- `runtime_provider = minimax`

and the exported runtime artifact's `runtime_profile.json` now preserves `require_verified_terminal: true` as a boolean.
