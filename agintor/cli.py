from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Optional

import typer

from .artifacts import ArtifactAllocator, ArtifactMode, WorkspaceLease
from .benchmarks import load_suite
from .exceptions import AgintorError
from .evaluator import RuntimeEvaluator
from .evolution import EvolutionEngine
from .project import init_runtime as init_runtime_dir, write_demo_suite
from .providers import build_provider
from .runtime_api import (
    load_solve_request,
    runtime_solve_request_for_task,
    runtime_solve_request_for_user_request,
)
from .runtime_builder import build_runtime_from_goal
from .runtime_host import RuntimeHost
from .runtime_profile import RUNTIME_PROFILE_FILE, RuntimeProfile, load_runtime_profile


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
    raise typer.BadParameter("provide either a prompt argument or --prompt-file")


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
) -> None:
    if task_id and (prompt or prompt_file):
        raise typer.BadParameter("use either benchmark mode with <task_id> or prompt mode with --prompt / --prompt-file")
    if not task_id and not (prompt or prompt_file):
        raise typer.BadParameter("provide either <task_id> for benchmark mode or --prompt / --prompt-file for user-request mode")
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
            mode = "user_request"
            runtime_request = runtime_solve_request_for_user_request(
                runtime_backend=runtime_backend,
                seed=seed,
                solve_request=solve_request,
            )
        response = host.solve(
            runtime_dir,
            runtime_request,
            provider=provider_impl,
            runtime_profile=runtime_profile,
        )
        payload = {
            "mode": mode,
            "task_id": task.task_id if task_id else None,
            "suite": suite if task_id else None,
            "partition": resolved_partition if task_id else None,
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
    prompt: Optional[str] = typer.Argument(None),
    destination: str = typer.Option(..., "--destination"),
    prompt_file: Optional[str] = typer.Option(None, "--prompt-file"),
    steps: int = typer.Option(10, "--steps"),
    provider: Optional[str] = typer.Option(None, "--provider", "--agintor-provider"),
    api_key_file: Optional[str] = typer.Option(None, "--api-key-file"),
    profile: Optional[str] = typer.Option(None, "--profile"),
    mutator: str = typer.Option("heuristic", "--mutator"),
    workspace: Optional[str] = typer.Option(None, "--workspace"),
    artifact_mode: ArtifactMode = typer.Option(ArtifactMode.ALWAYS, "--artifact-mode"),
    runtime_backend: str = typer.Option("local", "--runtime-backend"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    prompt_text = _load_prompt_input(prompt, prompt_file)
    runtime_profile = load_runtime_profile(profile_path=profile) if profile is not None else None
    provider_impl = _build_provider(provider, api_key_file, runtime_profile, default_to_runtime_profile=True)
    workspace_lease = _resolve_workspace(workspace, "build", artifact_mode)
    workspace_path = workspace_lease.path
    failed = True
    try:
        result = build_runtime_from_goal(
            prompt_text,
            destination=destination,
            workspace=workspace_path,
            provider=provider_impl,
            steps=steps,
            mutator_type=mutator,
            profile_path=profile,
            runtime_backend=runtime_backend,
            artifact_mode=artifact_mode,
            force=force,
        )
        failed = False
    except (AgintorError, RuntimeError, ValueError, FileExistsError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    finally:
        workspace_lease.release(failed=failed)
    typer.echo(json.dumps(result.__dict__, indent=2, sort_keys=True))


if __name__ == "__main__":
    app()
