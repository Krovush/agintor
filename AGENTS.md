# Repository Guidelines

## Project Structure & Module Organization
Core code lives in `agintor/` as a Python package. Key modules include CLI wiring in `agintor/cli.py`, runtime/evolution logic in `agintor/evolution.py` and `agintor/evaluator.py`, and provider integration in `agintor/providers.py`. Runtime templates are stored under `agintor/templates/baseline_runtime/`.

Tests are in `tests/` and mirror major user flows (`test_cli.py`, `test_core.py`, `test_evolution.py`). Keep new tests close to the behavior they validate. Project metadata and tooling configuration are in `pyproject.toml`.

## Build, Test, and Development Commands
- `python -m venv .venv; .\.venv\Scripts\Activate.ps1` (Windows): create and activate a virtual environment.
- `pip install -e ".[dev]"`: install package in editable mode with test dependencies.
- `pytest -q`: run the default fast test suite.
- `pytest -q -m live_openai`: run live OpenAI integration tests (requires credentials).
- `agintor init-runtime .tmp/runtime --write-demo-suite .tmp/demo_suite.json`: scaffold a runtime and demo suite.
- `agintor eval .tmp/runtime --suite .tmp/demo_suite.json --seeds 0`: run an evaluation from the CLI.

## Coding Style & Naming Conventions
Use Python 3.11+ features, 4-space indentation, and explicit type hints (consistent with existing modules). Follow PEP 8 naming:
- `snake_case` for functions/variables/modules
- `PascalCase` for classes
- `UPPER_SNAKE_CASE` for constants

Keep functions focused and side-effect boundaries clear (especially in evaluator/provider code paths).

## Testing Guidelines
Use `pytest` with files named `tests/test_*.py` and test functions named `test_*`. Prefer deterministic unit tests; isolate filesystem state with `tmp_path`. Mark paid/external tests with `@pytest.mark.live_openai` and gate them with environment checks (`OPENAI_API_KEY`, optional `AGINTOR_RUN_LIVE_RUNTIME=1`).

## Commit & Pull Request Guidelines
History follows Conventional Commit prefixes (`feat:`, `docs:`). Use concise, imperative summaries, for example: `feat: add runtime seed validation`.

PRs should include:
- clear problem/solution description
- linked issue or task ID (if available)
- test evidence (`pytest -q` output or equivalent)
- sample CLI output when behavior changes user-facing JSON or commands

## Security & Configuration Tips
Never commit secrets. Provide API keys via environment variables (for example `OPENAI_API_KEY`) and keep temporary runtime outputs in local scratch directories (for example `.tmp/`, `.agintor_runs/`).
