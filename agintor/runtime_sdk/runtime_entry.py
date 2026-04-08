from __future__ import annotations

import argparse
import json
from pathlib import Path

from .artifacts import ArtifactMode
from .exceptions import AgintorError
from .providers import build_provider_from_payload
from .pydantic_compat import model_dump, model_validate
from .runner import TaskRuntime
from .runtime_api import (
    benchmark_task_to_solve_request,
    runtime_solve_failure_response,
    solve_request_to_task,
    solve_result_from_run_result_with_context,
)
from .runtime_loader import load_runtime
from .runtime_profile import load_runtime_profile
from .schemas import (
    BenchmarkTask,
    CapabilityExchange,
    InspectRequest,
    RunResult,
    RuntimeBatchRequest,
    RuntimeBatchResponse,
    RuntimeSolveRequest,
    RuntimeSolveResponse,
    RuntimeTaskInvocation,
    SolveRequest,
)
from .shell import FixedShell


def _solve_failure_code(exc: Exception) -> str:
    text = str(exc).strip().lower()
    if "authentication" in text or "unauthorized" in text or "401" in text:
        return "provider_authentication_failed"
    if "not installed" in text:
        return "provider_dependency_missing"
    if "credential" in text or "api key" in text:
        return "missing_provider_credentials"
    return "solve_failure"


def _should_shape_solve_failure(exc: Exception) -> bool:
    if isinstance(exc, AgintorError):
        return True
    module_name = str(exc.__class__.__module__ or "").strip().lower()
    message = f"{exc.__class__.__name__}: {str(exc).strip()}".lower()
    if module_name.startswith(("anthropic", "openai")):
        return True
    return any(
        token in message
        for token in ("credential", "api key", "authentication", "unauthorized", "provider")
    )


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
    provider_payload_data = json.loads(Path(args.provider_json).read_text(encoding="utf-8"))
    provider = build_provider_from_payload(provider_payload_data, provider_profile=runtime_profile.runtime_provider)
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


def _solve(args: argparse.Namespace) -> int:
    request = model_validate(
        RuntimeSolveRequest,
        json.loads(Path(args.input_json).read_text(encoding="utf-8")),
    )
    runtime_profile = load_runtime_profile(args.runtime_dir, profile_path=args.profile_json)
    runtime = load_runtime(
        args.runtime_dir,
        runtime_profile=runtime_profile,
        runtime_backend=request.runtime_backend,
    )
    capability_exchange = CapabilityExchange(**model_dump(runtime.capability_exchange))
    if request.mode == "benchmark":
        if request.task is None:
            raise ValueError("benchmark solve requires a task payload")
        task = model_validate(BenchmarkTask, model_dump(request.task))
        solve_request = benchmark_task_to_solve_request(task, request_id=request.request_id)
    else:
        if request.solve_request is None:
            raise ValueError("user_request solve requires a solve_request payload")
        solve_request = model_validate(SolveRequest, model_dump(request.solve_request))
        task = solve_request_to_task(solve_request)
    provider_payload_data = json.loads(Path(args.provider_json).read_text(encoding="utf-8"))
    provider = None
    try:
        provider = build_provider_from_payload(provider_payload_data, provider_profile=runtime_profile.runtime_provider)
        shell = FixedShell(
            Path(args.workspace) / f"seed_{request.seed}",
            artifact_mode=ArtifactMode(args.artifact_mode),
        )
        runner = TaskRuntime(
            runtime,
            shell,
            provider,
            budget_overrides=request.budget_overrides,
            runtime_profile=runtime_profile,
        )
        run_result = runner.run_task(task, request.seed)
        solve_result = solve_result_from_run_result_with_context(
            solve_request,
            run_result,
            runtime.runtime_hash,
            mode=request.mode,
            provider_usage=provider.usage_summary(),
        )
        response = RuntimeSolveResponse(
            request_id=request.request_id,
            capability_exchange=capability_exchange,
            solve_result=solve_result,
        )
    except Exception as exc:
        if not _should_shape_solve_failure(exc):
            raise
        summary = str(exc).strip()
        if summary:
            summary = f"{exc.__class__.__name__}: {summary}"
        else:
            summary = exc.__class__.__name__
        response = runtime_solve_failure_response(
            solve_request,
            runtime.runtime_hash,
            capability_exchange,
            mode=request.mode,
            summary=summary,
            provider_usage=provider.usage_summary() if provider is not None else {},
            fault_code=_solve_failure_code(exc),
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

    solve_parser = subparsers.add_parser("solve")
    solve_parser.add_argument("--runtime-dir", required=True)
    solve_parser.add_argument("--input-json", required=True)
    solve_parser.add_argument("--provider-json", required=True)
    solve_parser.add_argument("--profile-json")
    solve_parser.add_argument("--workspace", required=True)
    solve_parser.add_argument("--artifact-mode", default=ArtifactMode.NONE.value)
    solve_parser.add_argument("--output-json", required=True)

    args = parser.parse_args(argv)
    if args.command == "inspect":
        return _inspect_runtime(args)
    if args.command == "run-batch":
        return _run_batch(args)
    if args.command == "solve":
        return _solve(args)
    raise ValueError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
