# Repository Guidelines

## Rules

- NEVER implement toy demos, hotfixes, fallbacks, or temporary patches. Fix root causes only, refactor the architecture when needed.
- Do not preserve backward compatibility for disposable MVP checkpoints, traces, or exported runtimes.
- Keep prompts, plans, handoffs, and summaries recipient-focused. Omit planning history, meta-commentary, and assumptions the next agent does not need.
- All planning and development-oriented documents belong in `C:\Users\yaros\Desktop\Agintor MVP\Dev Docs`.

## Project Structure & Module Organization

`agintor/` is the Python 3.12 application package. Shared Pydantic boundaries live in `agintor/contracts/`; factory construction is in `agintor/factory/`; execution is split across `agintor/runtime/api/`, `host/`, `kernel/`, and `langgraph/`; evaluation, search, oracle, storage, tracing, and provider integrations have matching top-level packages. Runtime JSON and prompt assets belong in `agintor/templates/`. Tests mirror these concerns under `tests/`, with focused suites such as `tests/runtime_host/` and `tests/container_runtime/`.

Use `Dev Docs/REPO_MAP.md` for current orientation and `Dev Docs/DEFERRED_ISSUES_LEDGER.md` for known work. Material under `Dev Docs/Archive Only - Zero Authority/` is historical. `TradingAgents/` is a separate reference checkout; do not treat it as part of the main package.

## Build, Test, and Development Commands

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e ".[dev]"
pytest
pytest tests/runtime_host/test_resume.py
pytest --cov=agintor --cov-report=term-missing
python -m pip wheel . --no-deps -w dist
agintor --help
```

The editable install provides the `agintor` CLI. Plain `pytest` runs the fast offline suite; project defaults exclude `heavy`, `docker`, and `live_openai` tests. Run those markers explicitly only when their services and credentials are available.

## Coding Style & Naming Conventions

Use four-space indentation, PEP 8 spacing, modern type annotations, and small single-purpose modules. Name modules, functions, and variables with `snake_case`; classes and Pydantic models with `PascalCase`; constants with `UPPER_SNAKE_CASE`. Prefer `pathlib.Path`, explicit boundary validation, and existing contract types over unstructured dictionaries. No formatter or linter is configured, so match nearby code and keep imports organized.

## Testing Guidelines

Pytest 8 is the test framework. Name files `test_<behavior>.py` and tests `test_<expected_outcome>`. Add regression coverage beside the affected subsystem and keep default tests deterministic and offline. There is no enforced coverage percentage; use the coverage command above to identify missed paths.

## Commit & Pull Request Guidelines

Recent history uses short imperative subjects such as `Implement oracle phase 1 validation contracts` and `Add repo patch proof lane evidence manifest`. Keep commits focused and avoid mixing generated artifacts with source changes. Pull requests should explain the behavior change, identify affected contracts or runtime paths, link the issue or ledger item, and list exact tests run. Include CLI output or screenshots only when they clarify user-visible behavior.

## Security & Configuration

Never commit API keys, `.env` files, generated runtimes, traces, or sealed oracle data. Supply provider credentials through documented environment variables such as `OPENAI_API_KEY` or configured key-file variables, and use synthetic fixtures in tests.
