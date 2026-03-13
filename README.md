# Agintor

Agintor is an MVP for bounded evolutionary search over executable agent runtimes across topology, memory, tooling, and control.

The canonical product and behavior contract for this repository lives in [PROJECT TARGET SPEC.md](PROJECT TARGET SPEC.md). This README exists so packaging metadata remains valid and to point contributors at the spec-first definition of the system.

## Development

- Install: `pip install -e .[dev]`
- Run tests: `pytest`
- CLI entrypoint: `agintor`

## Provider Boundaries

- Agintor's own control-plane runs (`evolve`, `build-runtime`) use the CLI-selected Agintor provider.
- Exported MAS runtimes carry their own runtime-provider contract in `runtime_profile.json`.
- The baseline exported runtime now defaults to MiniMax via `AGINTOR_MAS_MINIMAX_*` env vars, while Agintor's OpenAI control-plane settings stay under `OPENAI_API_KEY` and `AGINTOR_OPENAI_*`.
- Use `--provider local` on `solve` or `eval` when you want deterministic offline checks instead of the embedded runtime-provider contract.
