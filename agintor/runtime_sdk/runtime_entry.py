from __future__ import annotations

import argparse
import json
from pathlib import Path

from .artifacts import ArtifactMode
from .providers import build_provider_from_payload
from .pydantic_compat import model_dump, model_validate
from .runner import TaskRuntime
from .runtime_loader import load_runtime
from .runtime_profile import load_runtime_profile
from .schemas import (
    BenchmarkTask,
    CapabilityExchange,
    InspectRequest,
    RunResult,
    RuntimeBatchRequest,
    RuntimeBatchResponse,
    RuntimeTaskInvocation,
)
from .shell import FixedShell


def _inspect_runtime(args: argparse.Namespace) -> int:
    request = model_validate(
        InspectRequest,
        json.loads(Path(args.input_json).read_text(encoding="utf-8")),
    )
    runtime = load_runtime(args.runtime_dir, runtime_backend=request.requested_backend)
    if runtime.manifest.metadata.get("runtime_abi") != request.expected_runtime_abi:
        raise ValueError("runtime ABI mismatch during inspect")
    if request.expected_kernel_version and runtime.kernel_manifest.kernel_version != request.expected_kernel_version:
        raise ValueError("kernel version mismatch during inspect")
    if (
        request.expected_storage_schema_version
        and runtime.kernel_manifest.storage_schema_version != request.expected_storage_schema_version
    ):
        raise ValueError("storage schema mismatch during inspect")
    payload = model_dump(runtime.capability_exchange)
    Path(args.output_json).write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return 0


def _run_batch(args: argparse.Namespace) -> int:
    request = model_validate(
        RuntimeBatchRequest,
        json.loads(Path(args.input_json).read_text(encoding="utf-8")),
    )
    runtime_profile = load_runtime_profile(args.runtime_dir, profile_path=args.profile_json)
    provider_payload = json.loads(Path(args.provider_json).read_text(encoding="utf-8"))
    provider = build_provider_from_payload(provider_payload, provider_profile=runtime_profile.runtime_provider)
    runtime = load_runtime(
        args.runtime_dir,
        runtime_profile=runtime_profile,
        runtime_backend=request.runtime_backend,
    )
    results: list[RunResult] = []
    runners_by_seed: dict[int, TaskRuntime] = {}
    for invocation_payload in request.invocations:
        invocation = model_validate(RuntimeTaskInvocation, model_dump(invocation_payload))
        runner = runners_by_seed.get(invocation.seed)
        if runner is None:
            shell = FixedShell(
                Path(args.workspace) / f"seed_{invocation.seed}",
                artifact_mode=ArtifactMode(args.artifact_mode),
            )
            runner = TaskRuntime(
                runtime,
                shell,
                provider,
                budget_overrides=request.budget_overrides,
                runtime_profile=runtime_profile,
            )
            runners_by_seed[invocation.seed] = runner
        results.append(runner.run_task(model_validate(BenchmarkTask, model_dump(invocation.task)), invocation.seed))
    response = RuntimeBatchResponse(
        request_id=request.request_id,
        capability_exchange=CapabilityExchange(**model_dump(runtime.capability_exchange)),
        run_results=results,
        provider_usage=provider.usage_summary(),
    )
    Path(args.output_json).write_text(json.dumps(model_dump(response), indent=2, sort_keys=True), encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m agintor_runtime.runtime_entry")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("--runtime-dir", required=True)
    inspect_parser.add_argument("--input-json", required=True)
    inspect_parser.add_argument("--output-json", required=True)

    run_batch = subparsers.add_parser("run-batch")
    run_batch.add_argument("--runtime-dir", required=True)
    run_batch.add_argument("--input-json", required=True)
    run_batch.add_argument("--provider-json", required=True)
    run_batch.add_argument("--profile-json")
    run_batch.add_argument("--workspace", required=True)
    run_batch.add_argument("--artifact-mode", default=ArtifactMode.NONE.value)
    run_batch.add_argument("--output-json", required=True)

    args = parser.parse_args(argv)
    if args.command == "inspect":
        return _inspect_runtime(args)
    if args.command == "run-batch":
        return _run_batch(args)
    raise ValueError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
