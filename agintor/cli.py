from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any, Optional

import typer

from .artifacts import ArtifactAllocator, ArtifactMode, WorkspaceLease
from .benchmarks import load_suite
from .exceptions import AgintorError
from .evaluator import RuntimeEvaluator
from .evolution import EvolutionEngine
from .factory_chat_store import FactoryChatStore
from .project import init_runtime as init_runtime_dir, write_demo_suite
from .providers import build_provider
from .runtime_api import (
    load_solve_request,
    runtime_solve_request_for_task,
    runtime_solve_request_for_user_request,
)
from .runtime_builder import apply_factory_message
from .runtime_host import RuntimeHost
from .runtime_loader import load_runtime
from .runtime_profile import RUNTIME_PROFILE_FILE, RuntimeProfile, load_runtime_profile
from .runtime_session_store import RuntimeSessionStore
from .schemas import RuntimeSessionMessage
from .utils import now_ts


app = typer.Typer(add_completion=False, help="Agintor CLI MVP")



def _parse_seeds(text: str) -> list[int]:
    return [int(part.strip()) for part in text.split(",") if part.strip()]


def _reference_runtime_dir(provider_name: str, runtime_dir: str) -> Path | None:
    if provider_name == "local":
        return Path(runtime_dir)
    return None


def _usage_delta(before: dict[str, float | int], after: dict[str, float | int]) -> dict[str, float | int]:
    delta: dict[str, float | int] = {}
    for key in sorted(set(before) | set(after)):
        previous = before.get(key, 0)
        current = after.get(key, 0)
        if isinstance(previous, (int, float)) and isinstance(current, (int, float)):
            delta[key] = current - previous
        else:
            delta[key] = current
    return delta


def _supported_kwargs(callable_obj: object, **kwargs: object) -> dict[str, object]:
    try:
        params = inspect.signature(callable_obj).parameters
    except (TypeError, ValueError):
        return {}
    return {key: value for key, value in kwargs.items() if key in params}


def _build_provider(
    provider: Optional[str],
    api_key_file: Optional[str],
    runtime_profile: Optional[RuntimeProfile],
    *,
    default_to_runtime_profile: bool = False,
) -> object:
    if provider is None:
        if default_to_runtime_profile and runtime_profile is not None:
            return build_provider(
                runtime_profile.runtime_provider.name,
                provider_profile=runtime_profile.runtime_provider,
                api_key_file=api_key_file,
            )
        provider = "local"
    provider_profile = None
    if (
        default_to_runtime_profile
        and runtime_profile is not None
        and runtime_profile.runtime_provider.name == provider
    ):
        provider_profile = runtime_profile.runtime_provider
    return build_provider(provider, provider_profile=provider_profile, api_key_file=api_key_file)


def _load_prompt_input(prompt: Optional[str], prompt_file: Optional[str]) -> str:
    if prompt and prompt.strip():
        return prompt.strip()
    if prompt_file:
        return Path(prompt_file).read_text(encoding="utf-8").strip()
    raise typer.BadParameter("provide either --prompt or --prompt-file")


def _latest_session_run_for_message(
    host: RuntimeHost,
    session_message: RuntimeSessionMessage,
) -> dict[str, Any] | None:
    run_store = getattr(host, "run_store", None)
    runs_root = getattr(run_store, "runs_root", None)
    if run_store is None or runs_root is None:
        return None
    request_id = str(session_message.request_id or "").strip()
    session_id = str(session_message.session_id or "").strip()
    message_id = str(session_message.message_id or "").strip()
    message_index = int(session_message.message_index)
    candidates: list[tuple[float, dict[str, Any]]] = []
    for manifest_path in Path(runs_root).glob("run.*/run_manifest.json"):
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(payload.get("request_id") or "") != request_id and str(payload.get("evaluation_unit_id") or "") != request_id:
            continue
        trace_context = payload.get("trace_context")
        if not isinstance(trace_context, dict):
            continue
        if str(trace_context.get("runtime_session_id") or "").strip() != session_id:
            continue
        if str(trace_context.get("runtime_message_id") or "").strip() != message_id:
            continue
        try:
            trace_message_index = int(trace_context.get("runtime_message_index"))
        except (TypeError, ValueError):
            continue
        if trace_message_index != message_index:
            continue
        candidates.append((float(payload.get("updated_at") or payload.get("created_at") or 0.0), payload))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def _record_failed_session_message(
    *,
    host: RuntimeHost,
    session_store: RuntimeSessionStore,
    session_message: RuntimeSessionMessage,
    prompt_text: str,
    request_payload: dict[str, Any],
) -> None:
    run_manifest = _latest_session_run_for_message(host, session_message)
    checkpoint_ref = str((run_manifest or {}).get("latest_checkpoint_ref") or "").strip()
    lifecycle_state = "paused" if checkpoint_ref and (run_manifest or {}).get("lifecycle_state") == "paused" else "failed"
    session_message.lifecycle_state = lifecycle_state
    session_message.checkpoint_ref = checkpoint_ref or None
    session_store.record_message(
        session_message.session_id,
        session_message,
        prompt_text=prompt_text,
        request_payload=request_payload,
        response=None,
        result=None,
    )


def _resolve_workspace(workspace: Optional[str], purpose: str, artifact_mode: ArtifactMode) -> WorkspaceLease:
    return ArtifactAllocator.resolve().workspace(
        workspace,
        purpose=purpose,
        mode=artifact_mode,
        prefix=purpose,
    )


def _resolve_benchmark_task(benchmark, task_id: str) -> tuple[object, str]:
    for candidate_partition in ("train", "val", "test", "proxy"):
        for candidate in benchmark.all_tasks(candidate_partition):
            if candidate.task_id == task_id:
                return candidate, candidate_partition
    raise typer.BadParameter(f"task {task_id!r} was not found in suite {benchmark.name!r}")


@app.command("init-runtime")
def init_runtime_cmd(destination: str, force: bool = typer.Option(False, "--force"), write_suite: Optional[str] = typer.Option(None, "--write-demo-suite")) -> None:
    path = init_runtime_dir(destination, force=force)
    payload = {"runtime_dir": str(path), "profile_path": str(path / RUNTIME_PROFILE_FILE)}
    if write_suite is not None:
        suite_path = write_demo_suite(write_suite)
        payload["suite_path"] = str(suite_path)
    typer.echo(json.dumps(payload, indent=2))


@app.command("solve")
def solve_cmd(
    runtime_dir: str,
    task_id: Optional[str] = typer.Argument(None),
    suite: str = typer.Option("demo", "--suite"),
    partition: str = typer.Option("train", "--partition"),
    prompt: Optional[str] = typer.Option(None, "--prompt"),
    prompt_file: Optional[str] = typer.Option(None, "--prompt-file"),
    seed: int = typer.Option(0, "--seed"),
    provider: Optional[str] = typer.Option(None, "--provider", "--runtime-provider"),
    api_key_file: Optional[str] = typer.Option(None, "--api-key-file"),
    profile: Optional[str] = typer.Option(None, "--profile"),
    workspace: Optional[str] = typer.Option(None, "--workspace"),
    artifact_mode: ArtifactMode = typer.Option(ArtifactMode.NONE, "--artifact-mode"),
    runtime_backend: str = typer.Option("local", "--runtime-backend"),
    session: Optional[str] = typer.Option(None, "--session", help="Continue an existing runtime chat session id"),
    new_session: bool = typer.Option(False, "--new-session", help="Force-allocate a fresh chat session id"),
) -> None:
    if task_id and (prompt or prompt_file):
        raise typer.BadParameter("use either benchmark mode with <task_id> or prompt mode with --prompt / --prompt-file")
    if not task_id and not (prompt or prompt_file):
        raise typer.BadParameter("provide either <task_id> for benchmark mode or --prompt / --prompt-file for chat mode")
    if task_id and (session or new_session):
        raise typer.BadParameter("benchmark mode does not support runtime chat sessions")
    if session and new_session:
        raise typer.BadParameter("--session and --new-session are mutually exclusive")
    runtime_profile = load_runtime_profile(runtime_dir, profile_path=profile)
    provider_impl = _build_provider(provider, api_key_file, runtime_profile, default_to_runtime_profile=True)
    if workspace is None:
        allocator = ArtifactAllocator.resolve()
        workspace_lease = allocator.explicit_workspace(allocator.ensure_purpose_root("solve"), purpose="solve")
    else:
        workspace_lease = _resolve_workspace(workspace, "solve", artifact_mode)
    workspace_path = workspace_lease.path
    failed = True
    try:
        host = RuntimeHost(
            workspace_path,
            runtime_backend=runtime_backend,
            artifact_mode=artifact_mode,
        )
        target = "runtime"
        runtime_session_payload: Optional[dict[str, Any]] = None
        session_message: Optional[RuntimeSessionMessage] = None
        session_store: Optional[RuntimeSessionStore] = None
        prompt_text: Optional[str] = None
        if task_id:
            benchmark = load_suite(suite)
            task, resolved_partition = _resolve_benchmark_task(benchmark, task_id)
            mode = "benchmark"
            runtime_request = runtime_solve_request_for_task(
                runtime_backend=runtime_backend,
                seed=seed,
                task=task,
            )
        else:
            solve_request = load_solve_request(prompt=prompt, prompt_file=prompt_file)
            prompt_text = solve_request.prompt
            mode = "user_request"
            session_store = RuntimeSessionStore(runtime_dir)
            loaded_runtime = load_runtime(
                runtime_dir,
                runtime_profile=runtime_profile,
                runtime_backend=runtime_backend,
            )
            runtime_hash = loaded_runtime.runtime_hash
            if session:
                identity = session_store.load_session(
                    session,
                    runtime_hash=runtime_hash,
                    runtime_backend=runtime_backend,
                )
            else:
                identity = session_store.create_session(
                    runtime_hash=runtime_hash,
                    runtime_backend=runtime_backend,
                )
            message_index = session_store.next_message_index(identity.session_id)
            previous_message = session_store.latest_message(identity.session_id)
            session_seed = session_store.seed_for_next_message(identity.session_id)
            message_id = session_store.allocate_message_id(
                identity.session_id,
                message_index=message_index,
                prompt=prompt_text,
            )
            parent_message_id = previous_message.message_id if previous_message is not None else None
            session_message = RuntimeSessionMessage(
                message_id=message_id,
                message_index=message_index,
                parent_message_id=parent_message_id,
                session_id=identity.session_id,
                request_id=solve_request.request_id,
                prompt=prompt_text,
                created_at=now_ts(),
            )
            runtime_request = runtime_solve_request_for_user_request(
                runtime_backend=runtime_backend,
                seed=seed,
                solve_request=solve_request,
                runtime_session_id=identity.session_id,
                runtime_message_id=message_id,
                runtime_message_index=message_index,
                session_seed=session_seed,
            )
            runtime_session_payload = {
                "session_id": identity.session_id,
                "message_id": message_id,
                "message_index": message_index,
                "parent_message_id": parent_message_id,
                "runtime_hash": runtime_hash,
                "runtime_backend": runtime_backend,
            }
        try:
            response = host.solve(
                runtime_dir,
                runtime_request,
                provider=provider_impl,
                runtime_profile=runtime_profile,
            )
        except Exception:
            if session_store is not None and session_message is not None:
                _record_failed_session_message(
                    host=host,
                    session_store=session_store,
                    session_message=session_message,
                    prompt_text=prompt_text or "",
                    request_payload=(runtime_request).model_dump(),
                )
            raise
        if session_store is not None and session_message is not None:
            solve_result = response.solve_result
            session_message.lifecycle_state = (
                "completed"
                if str(solve_result.run_lifecycle_state or "").lower() == "completed"
                else (
                    "paused"
                    if str(solve_result.run_lifecycle_state or "").lower() == "paused"
                    else (
                        "cancelled"
                        if str(solve_result.run_lifecycle_state or "").lower() == "cancelled"
                        else "failed"
                    )
                )
            )
            session_message.checkpoint_ref = solve_result.latest_checkpoint_ref or solve_result.checkpoint_ref
            session_store.record_message(
                session_message.session_id,
                session_message,
                prompt_text=prompt_text or "",
                request_payload=(runtime_request).model_dump(),
                response=response,
                result=solve_result,
            )
        payload = {
            "target": target,
            "mode": mode,
            "task_id": task.task_id if task_id else None,
            "suite": suite if task_id else None,
            "partition": resolved_partition if task_id else None,
            "runtime_session": runtime_session_payload,
            "request": (runtime_request).model_dump(),
            "capability_exchange": (response.capability_exchange).model_dump(),
            "solve_result": (response.solve_result).model_dump(),
        }
        typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        failed = False
    finally:
        workspace_lease.release(failed=failed)


@app.command("eval")
def eval_cmd(
    runtime_dir: str,
    suite: str = typer.Option("demo", "--suite"),
    partition: str = typer.Option("train", "--partition"),
    seeds: str = typer.Option("0,1,2", "--seeds"),
    provider: Optional[str] = typer.Option(None, "--provider", "--runtime-provider"),
    api_key_file: Optional[str] = typer.Option(None, "--api-key-file"),
    profile: Optional[str] = typer.Option(None, "--profile"),
    workspace: Optional[str] = typer.Option(None, "--workspace"),
    artifact_mode: ArtifactMode = typer.Option(ArtifactMode.ON_FAILURE, "--artifact-mode"),
    runtime_backend: str = typer.Option("local", "--runtime-backend"),
) -> None:
    benchmark = load_suite(suite)
    runtime_profile = load_runtime_profile(runtime_dir, profile_path=profile)
    provider_impl = _build_provider(provider, api_key_file, runtime_profile, default_to_runtime_profile=True)
    effective_provider = provider or runtime_profile.runtime_provider.name
    workspace_lease = _resolve_workspace(workspace, "eval", artifact_mode)
    workspace_path = workspace_lease.path
    failed = True
    try:
        evaluator = RuntimeEvaluator(
            benchmark,
            workspace_path,
            provider_impl,
            baseline_runtime_dir=_reference_runtime_dir(effective_provider, runtime_dir),
            runtime_backend=runtime_backend,
            artifact_mode=artifact_mode,
            **_supported_kwargs(RuntimeEvaluator, runtime_profile=runtime_profile, profile_path=profile),
        )
        evaluation = evaluator.evaluate_runtime(runtime_dir, partition=partition, seeds=_parse_seeds(seeds), use_cache=False)
        typer.echo(json.dumps({**(evaluation).model_dump(), "provider_usage": dict(getattr(evaluator, "last_provider_usage", {}))}, indent=2, sort_keys=True))
        failed = False
    finally:
        workspace_lease.release(failed=failed)


@app.command("evolve")
def evolve_cmd(
    runtime_dir: str,
    suite: str = typer.Option("demo", "--suite"),
    steps: int = typer.Option(10, "--steps"),
    provider: str = typer.Option("local", "--provider", "--agintor-provider"),
    api_key_file: Optional[str] = typer.Option(None, "--api-key-file"),
    profile: Optional[str] = typer.Option(None, "--profile"),
    mutator: str = typer.Option("heuristic", "--mutator"),
    workspace: Optional[str] = typer.Option(None, "--workspace"),
    artifact_mode: ArtifactMode = typer.Option(ArtifactMode.ALWAYS, "--artifact-mode"),
    runtime_backend: str = typer.Option("local", "--runtime-backend"),
) -> None:
    benchmark = load_suite(suite)
    runtime_profile = load_runtime_profile(runtime_dir, profile_path=profile)
    provider_impl = _build_provider(provider, api_key_file, runtime_profile, default_to_runtime_profile=False)
    workspace_lease = _resolve_workspace(workspace, "evolve", artifact_mode)
    workspace_path = workspace_lease.path
    failed = True
    try:
        engine = EvolutionEngine(
            benchmark,
            workspace_path,
            provider_impl,
            Path(runtime_dir),
            mutator_type=mutator,
            reference_runtime_dir=_reference_runtime_dir(provider, runtime_dir),
            runtime_backend=runtime_backend,
            artifact_mode=artifact_mode,
            **_supported_kwargs(EvolutionEngine, runtime_profile=runtime_profile, profile_path=profile),
        )
        usage_before = provider_impl.usage_summary()
        summary = engine.run(steps=steps)
        typer.echo(json.dumps({**summary.__dict__, "provider_usage": _usage_delta(usage_before, provider_impl.usage_summary())}, indent=2, sort_keys=True))
        failed = False
    finally:
        workspace_lease.release(failed=failed)


@app.command("build-runtime")
def build_runtime_cmd(
    project_dir_or_prompt: Optional[str] = typer.Argument(
        None,
        help="Project directory, or the goal text when --destination is supplied.",
    ),
    prompt: Optional[str] = typer.Option(
        None,
        "--prompt",
        help="Initial goal or follow-up instruction.",
    ),
    prompt_file: Optional[str] = typer.Option(None, "--prompt-file"),
    destination: Optional[str] = typer.Option(
        None,
        "--destination",
        help="Project directory for a new factory chat.",
    ),
    steps: int = typer.Option(10, "--steps"),
    provider: Optional[str] = typer.Option(None, "--provider", "--agintor-provider"),
    api_key_file: Optional[str] = typer.Option(None, "--api-key-file"),
    profile: Optional[str] = typer.Option(None, "--profile"),
    mutator: str = typer.Option("heuristic", "--mutator"),
    workspace: Optional[str] = typer.Option(None, "--workspace"),
    artifact_mode: ArtifactMode = typer.Option(ArtifactMode.ALWAYS, "--artifact-mode"),
    runtime_backend: Optional[str] = typer.Option(None, "--runtime-backend"),
) -> None:
    """Drive the factory chat for a project.

    The first call against a fresh project_dir creates a new factory chat and
    runs the initial build. Subsequent calls append a follow-up message that
    amends the prior goal and rebuilds the runtime in place.
    """
    if destination is not None:
        project_dir = destination
        if project_dir_or_prompt and (prompt or prompt_file):
            raise typer.BadParameter(
                "use either positional goal text or --prompt / --prompt-file, not both"
            )
        prompt_text = _load_prompt_input(project_dir_or_prompt or prompt, prompt_file)
    else:
        if not project_dir_or_prompt:
            raise typer.BadParameter("provide <project_dir> or use --destination <project_dir>")
        project_dir = project_dir_or_prompt
        prompt_text = _load_prompt_input(prompt, prompt_file)
    existing_chat = None
    chat_store = FactoryChatStore(project_dir)
    if chat_store.has_chat():
        existing_chat = chat_store.load_chat()
    if provider is None and existing_chat is not None:
        provider = existing_chat.agintor_provider
    if runtime_backend is None and existing_chat is not None:
        runtime_backend = existing_chat.runtime_backend
    if profile is not None and existing_chat is not None:
        raise typer.BadParameter(
            "factory follow-ups use the runtime profile pinned in the project; "
            "start a new project to use a different profile"
        )
    if existing_chat is not None:
        runtime_profile = load_runtime_profile(project_dir, profile_path=None)
    else:
        runtime_profile = load_runtime_profile(profile_path=profile) if profile is not None else None
    provider_impl = _build_provider(provider, api_key_file, runtime_profile, default_to_runtime_profile=True)
    workspace_lease = _resolve_workspace(workspace, "build", artifact_mode)
    workspace_path = workspace_lease.path
    failed = True
    try:
        outcome = apply_factory_message(
            project_dir,
            prompt_text,
            workspace=workspace_path,
            provider=provider_impl,
            steps=steps,
            mutator_type=mutator,
            profile_path=profile,
            runtime_backend=runtime_backend,
            artifact_mode=artifact_mode,
        )
        failed = False
    except (AgintorError, RuntimeError, ValueError, FileExistsError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    finally:
        workspace_lease.release(failed=failed)
    typer.echo(
        json.dumps(
            {
                "target": "factory",
                "factory_chat": {
                    "chat_id": outcome.chat.chat_id,
                    "project_dir": outcome.chat.project_dir,
                    "message_id": outcome.message.message_id,
                    "message_index": outcome.message.message_index,
                    "parent_message_id": outcome.message.parent_message_id,
                    "runtime_hash": outcome.message.leader_runtime_hash,
                    "runtime_dir": outcome.message.leader_runtime_dir,
                    "build_id": outcome.message.build_id,
                },
                "build": outcome.result.__dict__,
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    app()
