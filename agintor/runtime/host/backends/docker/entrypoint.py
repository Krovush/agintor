from __future__ import annotations

import argparse
import inspect
import json
from pathlib import Path

from .....providers import build_provider_from_payload
from ....kernel.facade import TaskRuntime
from ....profile import load_runtime_profile
from ....loader import load_runtime
from .....contracts import BenchmarkTask, RunResult
from ....kernel.shell import FixedShell


def _supported_kwargs(callable_obj, **kwargs):
    try:
        params = inspect.signature(callable_obj).parameters
    except (TypeError, ValueError):
        return {}
    return {key: value for key, value in kwargs.items() if key in params}


def _run_runtime_batch(args: argparse.Namespace) -> int:
    task_runs = json.loads(Path(args.task_runs_json).read_text(encoding="utf-8"))
    if not isinstance(task_runs, list):
        raise ValueError("task runs payload must be a JSON array")
    runtime_profile = load_runtime_profile(args.runtime_dir, profile_path=args.profile_json)
    provider_payload = json.loads(Path(args.provider_json).read_text(encoding="utf-8"))
    provider = build_provider_from_payload(provider_payload)
    runtime = load_runtime(args.runtime_dir, runtime_profile=runtime_profile)
    results: list[RunResult] = []
    runners_by_seed: dict[int, TaskRuntime] = {}
    for item in task_runs:
        if not isinstance(item, dict):
            raise ValueError("task runs entries must be JSON objects")
        seed = int(item["seed"])
        task = (BenchmarkTask).model_validate(item["task"])
        runner = runners_by_seed.get(seed)
        if runner is None:
            shell = FixedShell(
                Path(args.workspace) / f"seed_{seed}",
                **_supported_kwargs(FixedShell, profile=runtime_profile),
            )
            runner = TaskRuntime(
                runtime,
                shell,
                provider,
                **_supported_kwargs(TaskRuntime, runtime_profile=runtime_profile),
            )
            runners_by_seed[seed] = runner
        results.append(runner.run_task(task, seed))
    output_path = Path(args.output_json)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps([(result).model_dump() for result in results], indent=2, sort_keys=True), encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agintor.runtime.host.backends.docker.entrypoint")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_batch = subparsers.add_parser("run-runtime-batch")
    run_batch.add_argument("--runtime-dir", required=True)
    run_batch.add_argument("--task-runs-json", required=True)
    run_batch.add_argument("--provider-json", required=True)
    run_batch.add_argument("--profile-json")
    run_batch.add_argument("--output-json", required=True)
    run_batch.add_argument("--workspace", required=True)

    args = parser.parse_args(argv)
    if args.command == "run-runtime-batch":
        return _run_runtime_batch(args)
    raise ValueError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
