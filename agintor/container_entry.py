from __future__ import annotations

import argparse
import inspect
import json
from pathlib import Path

from .providers import build_provider
from .pydantic_compat import model_dump, model_validate
from .runner import TaskRuntime
from .runtime_profile import load_runtime_profile
from .runtime_loader import load_runtime
from .schemas import BenchmarkTask, RunResult
from .shell import FixedShell


def _supported_kwargs(callable_obj, **kwargs):
    try:
        params = inspect.signature(callable_obj).parameters
    except (TypeError, ValueError):
        return {}
    return {key: value for key, value in kwargs.items() if key in params}


def _run_runtime_unit(args: argparse.Namespace) -> int:
    tasks = [
        model_validate(BenchmarkTask, item)
        for item in json.loads(Path(args.tasks_json).read_text(encoding="utf-8"))
    ]
    runtime_profile = load_runtime_profile(args.runtime_dir, profile_path=args.profile_json)
    provider_profile = None
    if runtime_profile.runtime_provider.name == args.provider:
        provider_profile = runtime_profile.runtime_provider
    provider = build_provider(
        args.provider,
        provider_profile=provider_profile,
        api_key_file=args.api_key_file,
    )
    runtime = load_runtime(args.runtime_dir, runtime_profile=runtime_profile)
    shell = FixedShell(
        Path(args.workspace),
        **_supported_kwargs(FixedShell, profile=runtime_profile),
    )
    runner = TaskRuntime(
        runtime,
        shell,
        provider,
        **_supported_kwargs(TaskRuntime, runtime_profile=runtime_profile),
    )
    results: list[RunResult] = [runner.run_task(task, int(args.seed)) for task in tasks]
    output_path = Path(args.output_json)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps([model_dump(result) for result in results], indent=2, sort_keys=True), encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agintor.container_entry")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_unit = subparsers.add_parser("run-runtime-unit")
    run_unit.add_argument("--runtime-dir", required=True)
    run_unit.add_argument("--tasks-json", required=True)
    run_unit.add_argument("--seed", required=True)
    run_unit.add_argument("--provider", required=True)
    run_unit.add_argument("--api-key-file")
    run_unit.add_argument("--profile-json")
    run_unit.add_argument("--output-json", required=True)
    run_unit.add_argument("--workspace", required=True)

    args = parser.parse_args(argv)
    if args.command == "run-runtime-unit":
        return _run_runtime_unit(args)
    raise ValueError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
