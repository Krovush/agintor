from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Mapping

from ...storage.artifacts import ArtifactMode
from ...core.exceptions import AgintorError, PromptAdaptationError
from ...providers import build_provider_from_payload
from ...storage.run_store import RunStore
from ..kernel.facade import TaskRuntime
from ..api import (
    batch_evaluation_unit_key,
    benchmark_task_to_solve_request,
    compile_execution_plan_from_solve_request,
    compile_execution_plan_from_task,
    reduce_grouped_run_results,
    resume_task_and_plan_from_checkpoint,
    runtime_solve_failure_response,
    solve_request_from_resume_checkpoint,
    solve_result_from_run_result_with_context,
    synthesize_blocked_episode_run,
)
from ..loader import load_runtime
from ..profile import load_runtime_profile
from ...contracts import (
    BenchmarkTask,
    CapabilityExchange,
    ExecutionPlan,
    InspectRequest,
    ResumeRequest,
    RunResult,
    RuntimeBatchRequest,
    RuntimeBatchResponse,
    RuntimeResumeRequest,
    RuntimeSolveRequest,
    RuntimeSolveResponse,
    RuntimeTaskInvocation,
    SolveRequest,
)
from ..kernel.shell import FixedShell
from ...utils import merge_provider_usage


def _solve_failure_code(exc: Exception) -> str:
    if isinstance(exc, PromptAdaptationError):
        return exc.failure_kind
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


def _latest_usable_checkpoint_ref(run_root: str) -> str | None:
    text = str(run_root or "").strip()
    if not text:
        return None
    try:
        return RunStore.from_run_root(text).latest_usable_checkpoint_ref(text)
    except Exception:
        return None


def _shape_batch_failure_run(
    invocation: RuntimeTaskInvocation,
    exc: Exception,
    *,
    runtime_hash: str = "",
) -> RunResult:
    summary = str(exc).strip()
    if summary:
        summary = f"{exc.__class__.__name__}: {summary}"
    else:
        summary = exc.__class__.__name__
    latest_checkpoint_ref = _latest_usable_checkpoint_ref(invocation.run_root)
    lifecycle_state = "paused" if latest_checkpoint_ref else "failed"
    return RunResult(
        request_id=invocation.request_id,
        plan_id="",
        run_id=invocation.run_id,
        run_root=invocation.run_root,
        attempt_id=invocation.attempt_id,
        runtime_hash=runtime_hash,
        runtime_backend=invocation.runtime_backend,
        task_id=invocation.task.task_id,
        seed=invocation.seed,
        artifact={"error": _solve_failure_code(exc), "message": summary},
        verifier_score=0.0,
        cost=0.0,
        latency=0.0,
        faults=1,
        trace=[],
        trace_context=invocation.trace_context,
        hard_invalid=False,
        invalid_reason=summary,
        failure_kind=_solve_failure_code(exc),
        latest_checkpoint_ref=latest_checkpoint_ref,
        checkpoint_ref=latest_checkpoint_ref,
        run_lifecycle_state=lifecycle_state,
        run_resumable=bool(latest_checkpoint_ref),
        run_prune_eligible=not bool(latest_checkpoint_ref),
        provider_usage={},
    )


def _request_envelope(bundle: Mapping[str, Any] | None) -> Mapping[str, Any]:
    if not isinstance(bundle, Mapping):
        return {}
    request_payload = bundle.get("request")
    if isinstance(request_payload, Mapping):
        return request_payload
    return bundle


def _inspect_runtime(args: argparse.Namespace) -> int:
    request = (InspectRequest).model_validate(json.loads(Path(args.input_json).read_text(encoding="utf-8")))
    runtime = load_runtime(args.runtime_dir, runtime_backend=request.requested_backend)
    if runtime.kernel_manifest.runtime_contract_version != request.expected_runtime_contract_version:
        raise ValueError("runtime contract mismatch during inspect")
    payload = (runtime.capability_exchange).model_dump()
    Path(args.output_json).write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    return 0


def _run_batch(args: argparse.Namespace) -> int:
    request = (RuntimeBatchRequest).model_validate(json.loads(Path(args.input_json).read_text(encoding="utf-8")))
    runtime_profile = load_runtime_profile(args.runtime_dir, profile_path=args.profile_json)
    provider_payload_data = json.loads(Path(args.provider_json).read_text(encoding="utf-8"))
    provider = build_provider_from_payload(provider_payload_data, provider_profile=runtime_profile.runtime_provider)
    runtime = load_runtime(
        args.runtime_dir,
        runtime_profile=runtime_profile,
        runtime_backend=request.runtime_backend,
    )
    results_by_index: dict[int, RunResult] = {}
    runners_by_group: dict[str, TaskRuntime] = {}
    indexed_invocations = [
        (index, (RuntimeTaskInvocation).model_validate(invocation_payload))
        for index, invocation_payload in enumerate(request.invocations)
    ]
    grouped: dict[str, list[tuple[int, RuntimeTaskInvocation]]] = {}
    for index, invocation in indexed_invocations:
        grouped.setdefault(batch_evaluation_unit_key(invocation), []).append((index, invocation))
    for group_key, rows in grouped.items():
        grouped_episode = bool(rows) and str(rows[0][1].episode_kind or "") == "transfer_episode"
        if grouped_episode:
            rows = sorted(
                rows,
                key=lambda item: (
                    int(item[1].episode_step_index or 0),
                    item[0],
                ),
            )
        blocking_run: RunResult | None = None
        for original_index, invocation in rows:
            selected_backend = str(invocation.runtime_backend or request.runtime_backend).strip().lower()
            if selected_backend != str(request.runtime_backend).strip().lower():
                raise ValueError(
                    f"batch invocation backend mismatch for {invocation.request_id}: "
                    f"{selected_backend!r} != {request.runtime_backend!r}"
                )
            runner = runners_by_group.get(group_key)
            if runner is None:
                run_store = RunStore.from_run_root(invocation.run_root) if invocation.run_root else None
                shell = FixedShell(
                    Path(invocation.run_root) / "attempts" / invocation.attempt_id / "workspace"
                    if invocation.run_root and invocation.attempt_id
                    else Path(args.workspace) / group_key.replace("/", "_"),
                    artifact_mode=ArtifactMode(args.artifact_mode),
                    run_store=run_store,
                    run_id=invocation.run_id,
                    attempt_id=invocation.attempt_id,
                )
                runner = TaskRuntime(
                    runtime,
                    shell,
                    provider,
                    budget_overrides=request.budget_overrides,
                    runtime_profile=runtime_profile,
                    runtime_backend=selected_backend,
                )
                runners_by_group[group_key] = runner
            if blocking_run is not None:
                results_by_index[original_index] = synthesize_blocked_episode_run(
                    invocation,
                    run_id=invocation.run_id,
                    run_root=invocation.run_root,
                    attempt_id=invocation.attempt_id,
                    blocking_run=blocking_run,
                )
                continue
            try:
                run_result = runner.run_task(
                    (BenchmarkTask).model_validate((invocation.task).model_dump()),
                    invocation.seed,
                    request_id=invocation.request_id,
                    trace_context=invocation.trace_context,
                )
            except Exception as exc:
                if not _should_shape_solve_failure(exc):
                    raise
                run_result = _shape_batch_failure_run(invocation, exc, runtime_hash=runtime.runtime_hash)
            results_by_index[original_index] = run_result
            if grouped_episode:
                reduced = reduce_grouped_run_results([run_result])
                if reduced["lifecycle_state"] in {"paused", "failed", "cancelled"}:
                    blocking_run = run_result
    results = [results_by_index[index] for index in sorted(results_by_index)]
    response = RuntimeBatchResponse(
        request_id=request.request_id,
        capability_exchange=CapabilityExchange(**(runtime.capability_exchange).model_dump()),
        run_results=results,
        provider_usage=merge_provider_usage(*(run.provider_usage for run in results)),
    )
    Path(args.output_json).write_text(json.dumps((response).model_dump(), indent=2, sort_keys=True), encoding="utf-8")
    return 0


def _solve(args: argparse.Namespace) -> int:
    request = (RuntimeSolveRequest).model_validate(json.loads(Path(args.input_json).read_text(encoding="utf-8")))
    runtime_profile = load_runtime_profile(args.runtime_dir, profile_path=args.profile_json)
    runtime = load_runtime(
        args.runtime_dir,
        runtime_profile=runtime_profile,
        runtime_backend=request.runtime_backend,
    )
    capability_exchange = CapabilityExchange(**(runtime.capability_exchange).model_dump())
    provider_payload_data = json.loads(Path(args.provider_json).read_text(encoding="utf-8"))
    provider = None
    solve_request: SolveRequest | None = None
    try:
        if request.mode == "benchmark":
            if request.task is None:
                raise ValueError("benchmark solve requires a task payload")
            task = (BenchmarkTask).model_validate((request.task).model_dump())
            solve_request = benchmark_task_to_solve_request(task, request_id=request.request_id)
            execution_plan = compile_execution_plan_from_task(
                task,
                request_id=request.request_id,
                seed=request.seed,
                runtime_hash=runtime.runtime_hash,
                runtime_dir=str(runtime.runtime_dir),
                trace_context=request.trace_context,
                budget_overrides=request.budget_overrides,
            )
        else:
            if request.solve_request is None:
                raise ValueError("user_request solve requires a solve_request payload")
            solve_request = (SolveRequest).model_validate((request.solve_request).model_dump())
            task, execution_plan = compile_execution_plan_from_solve_request(
                solve_request,
                seed=request.seed,
                runtime_hash=runtime.runtime_hash,
                runtime_dir=str(runtime.runtime_dir),
                trace_context=request.trace_context,
            )
        provider = build_provider_from_payload(provider_payload_data, provider_profile=runtime_profile.runtime_provider)
        run_store = RunStore.from_run_root(request.run_root) if request.run_root else None
        shell = FixedShell(
            Path(request.run_root) / "attempts" / request.attempt_id / "workspace"
            if request.run_root and request.attempt_id
            else Path(args.workspace) / f"seed_{request.seed}",
            artifact_mode=ArtifactMode(args.artifact_mode),
            run_store=run_store,
            run_id=request.run_id,
            attempt_id=request.attempt_id,
        )
        runner = TaskRuntime(
            runtime,
            shell,
            provider,
            budget_overrides=request.budget_overrides,
            runtime_profile=runtime_profile,
            runtime_backend=request.runtime_backend,
        )
        run_result = runner.run_task(
            task,
            request.seed,
            request_id=request.request_id,
            trace_context=request.trace_context,
            plan=execution_plan,
            session_seed=request.session_seed,
        )
        solve_result = solve_result_from_run_result_with_context(
            solve_request,
            run_result,
            runtime.runtime_hash,
            mode=request.mode,
            provider_usage=run_result.provider_usage,
        )
        if (
            request.mode == "user_request"
            and str(run_result.run_lifecycle_state or run_result.lifecycle_state or "").lower() == "completed"
            and getattr(runtime, "runtime_spec", None) is None
        ):
            long_term, predictor, short_term_export = runner._export_post_message_state(run_result=run_result)
            solve_result.post_message_long_term_graph = long_term
            solve_result.post_message_predictor_snapshot = predictor
            solve_result.post_message_short_term_export = short_term_export
        response = RuntimeSolveResponse(
            request_id=request.request_id,
            capability_exchange=capability_exchange,
            solve_result=solve_result,
        )
    except Exception as exc:
        if not _should_shape_solve_failure(exc):
            raise
        if solve_request is None:
            solve_request = (
                benchmark_task_to_solve_request((BenchmarkTask).model_validate((request.task).model_dump()), request_id=request.request_id)
                if request.mode == "benchmark" and request.task is not None
                else (SolveRequest).model_validate((request.solve_request).model_dump())
                if request.solve_request is not None
                else SolveRequest(request_id=request.request_id, prompt="Runtime solve request")
            )
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
            run_id=request.run_id,
            run_root=request.run_root,
            attempt_id=request.attempt_id,
            latest_checkpoint_ref=_latest_usable_checkpoint_ref(request.run_root),
        )
    Path(args.output_json).write_text(json.dumps((response).model_dump(), indent=2, sort_keys=True), encoding="utf-8")
    return 0


def _resume(args: argparse.Namespace) -> int:
    request = (RuntimeResumeRequest).model_validate(json.loads(Path(args.input_json).read_text(encoding="utf-8")))
    runtime_profile = load_runtime_profile(args.runtime_dir, profile_path=args.profile_json)
    runtime = load_runtime(
        args.runtime_dir,
        runtime_profile=runtime_profile,
        runtime_backend=request.runtime_backend,
    )
    capability_exchange = CapabilityExchange(**(runtime.capability_exchange).model_dump())
    provider_payload_data = json.loads(Path(args.provider_json).read_text(encoding="utf-8"))
    provider = None
    solve_request: SolveRequest | None = None
    mode = "benchmark"
    try:
        provider = build_provider_from_payload(provider_payload_data, provider_profile=runtime_profile.runtime_provider)
        run_store = RunStore.from_run_root(request.run_root) if request.run_root else None
        shell = FixedShell(
            Path(request.run_root) / "attempts" / request.attempt_id / "workspace"
            if request.run_root and request.attempt_id
            else Path(args.workspace) / f"resume_{request.request_id}",
            artifact_mode=ArtifactMode(args.artifact_mode),
            run_store=run_store,
            run_id=request.run_id,
            attempt_id=request.attempt_id,
        )
        shell.configure_resume_checkpoint_store(request.checkpoint_store_dir)
        envelope = shell.load_checkpoint_envelope(
            checkpoint_ref=request.checkpoint_ref,
            run_ref=request.run_ref or request.run_id or request.run_root,
            checkpoint_store_dir=request.checkpoint_store_dir,
        )
        request_bundle = {}
        if run_store is not None:
            request_bundle = _request_envelope(run_store.load_request_bundle(run_store.run_root or request.run_root))
        source_checkpoint_ref = (
            str(Path(request.checkpoint_ref).expanduser().resolve())
            if str(request.checkpoint_ref or "").strip()
            else _latest_usable_checkpoint_ref(request.run_root or request.run_id or request.run_ref)
        )
        solve_request, rebound_envelope, _ = solve_request_from_resume_checkpoint(
            envelope,
            request_id_override=request.request_id or envelope.request_id,
            request_bundle=request_bundle,
            source_checkpoint_ref=source_checkpoint_ref,
            trace_context=request.trace_context,
        )
        _, checkpoint_plan = resume_task_and_plan_from_checkpoint(rebound_envelope)
        mode = "benchmark" if checkpoint_plan.origin.origin_kind == "benchmark" else "user_request"
        runner = TaskRuntime(
            runtime,
            shell,
            provider,
            runtime_profile=runtime_profile,
            runtime_backend=request.runtime_backend,
        )
        run_result = runner.resume_from_checkpoint(
            rebound_envelope,
            reconciliation_policy=request.reconciliation_policy,
        )
        solve_result = solve_result_from_run_result_with_context(
            solve_request,
            run_result,
            runtime.runtime_hash,
            mode=mode,
            provider_usage=run_result.provider_usage,
        )
        response = RuntimeSolveResponse(
            request_id=solve_request.request_id,
            capability_exchange=capability_exchange,
            solve_result=solve_result,
        )
    except Exception as exc:
        if not _should_shape_solve_failure(exc):
            raise
        if solve_request is None:
            solve_request = SolveRequest(
                request_id=request.request_id or request.run_id or request.run_ref or "resume",
                prompt="Runtime resume request",
            )
        summary = str(exc).strip()
        if summary:
            summary = f"{exc.__class__.__name__}: {summary}"
        else:
            summary = exc.__class__.__name__
        response = runtime_solve_failure_response(
            solve_request,
            runtime.runtime_hash,
            capability_exchange,
            mode=mode,
            summary=summary,
            provider_usage=provider.usage_summary() if provider is not None else {},
            fault_code=_solve_failure_code(exc),
            run_id=request.run_id,
            run_root=request.run_root,
            attempt_id=request.attempt_id,
            latest_checkpoint_ref=_latest_usable_checkpoint_ref(request.run_root),
        )
    Path(args.output_json).write_text(json.dumps((response).model_dump(), indent=2, sort_keys=True), encoding="utf-8")
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

    resume_parser = subparsers.add_parser("resume")
    resume_parser.add_argument("--runtime-dir", required=True)
    resume_parser.add_argument("--input-json", required=True)
    resume_parser.add_argument("--provider-json", required=True)
    resume_parser.add_argument("--profile-json")
    resume_parser.add_argument("--workspace", required=True)
    resume_parser.add_argument("--artifact-mode", default=ArtifactMode.NONE.value)
    resume_parser.add_argument("--output-json", required=True)

    args = parser.parse_args(argv)
    if args.command == "inspect":
        return _inspect_runtime(args)
    if args.command == "run-batch":
        return _run_batch(args)
    if args.command == "solve":
        return _solve(args)
    if args.command == "resume":
        return _resume(args)
    raise ValueError(args.command)


if __name__ == "__main__":
    raise SystemExit(main())
