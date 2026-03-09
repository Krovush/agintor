from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from .benchmarks import load_suite
from .exceptions import AgintorError
from .evaluator import RuntimeEvaluator
from .evolution import EvolutionEngine
from .project import init_runtime as init_runtime_dir, write_demo_suite
from .pydantic_compat import model_dump
from .providers import OpenAIProvider, build_provider
from .research import run_research_prompt
from .runtime_loader import load_runtime


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


def _build_provider(provider: str, api_key_file: Optional[str]) -> object:
    kwargs = {"api_key_file": api_key_file} if api_key_file else {}
    return build_provider(provider, **kwargs)


def _load_prompt_input(prompt: Optional[str], prompt_file: Optional[str]) -> str:
    if prompt and prompt.strip():
        return prompt.strip()
    if prompt_file:
        return Path(prompt_file).read_text(encoding="utf-8").strip()
    raise typer.BadParameter("provide either a prompt argument or --prompt-file")


@app.command("init-runtime")
def init_runtime_cmd(destination: str, force: bool = typer.Option(False, "--force"), write_suite: Optional[str] = typer.Option(None, "--write-demo-suite")) -> None:
    path = init_runtime_dir(destination, force=force)
    payload = {"runtime_dir": str(path)}
    if write_suite is not None:
        suite_path = write_demo_suite(write_suite)
        payload["suite_path"] = str(suite_path)
    typer.echo(json.dumps(payload, indent=2))


@app.command("solve")
def solve_cmd(
    runtime_dir: str,
    task_id: str,
    suite: str = typer.Option("demo", "--suite"),
    partition: str = typer.Option("train", "--partition"),
    seed: int = typer.Option(0, "--seed"),
    provider: str = typer.Option("local", "--provider"),
    api_key_file: Optional[str] = typer.Option(None, "--api-key-file"),
    workspace: str = typer.Option(".agintor_runs", "--workspace"),
) -> None:
    benchmark = load_suite(suite)
    task = benchmark.by_id(task_id)
    runtime = load_runtime(runtime_dir)
    provider_impl = _build_provider(provider, api_key_file)
    evaluator = RuntimeEvaluator(benchmark, Path(workspace), provider_impl, baseline_runtime_dir=_reference_runtime_dir(provider, runtime_dir))
    usage_before = provider_impl.usage_summary()
    evaluation = evaluator.evaluate_runtime(runtime_dir, partition=partition, seeds=[seed], use_cache=False, tasks_override=[task])
    typer.echo(json.dumps({
        "runtime_hash": runtime.runtime_hash,
        "task_id": task_id,
        "result": model_dump(evaluation.run_results[0]),
        "objective_scores": evaluation.objective_scores,
        "provider_usage": _usage_delta(usage_before, provider_impl.usage_summary()),
    }, indent=2, sort_keys=True))


@app.command("eval")
def eval_cmd(
    runtime_dir: str,
    suite: str = typer.Option("demo", "--suite"),
    partition: str = typer.Option("train", "--partition"),
    seeds: str = typer.Option("0,1,2", "--seeds"),
    provider: str = typer.Option("local", "--provider"),
    api_key_file: Optional[str] = typer.Option(None, "--api-key-file"),
    workspace: str = typer.Option(".agintor_runs", "--workspace"),
) -> None:
    benchmark = load_suite(suite)
    provider_impl = _build_provider(provider, api_key_file)
    evaluator = RuntimeEvaluator(benchmark, Path(workspace), provider_impl, baseline_runtime_dir=_reference_runtime_dir(provider, runtime_dir))
    usage_before = provider_impl.usage_summary()
    evaluation = evaluator.evaluate_runtime(runtime_dir, partition=partition, seeds=_parse_seeds(seeds), use_cache=False)
    typer.echo(json.dumps({**model_dump(evaluation), "provider_usage": _usage_delta(usage_before, provider_impl.usage_summary())}, indent=2, sort_keys=True))


@app.command("evolve")
def evolve_cmd(
    runtime_dir: str,
    suite: str = typer.Option("demo", "--suite"),
    steps: int = typer.Option(10, "--steps"),
    provider: str = typer.Option("local", "--provider"),
    api_key_file: Optional[str] = typer.Option(None, "--api-key-file"),
    mutator: str = typer.Option("heuristic", "--mutator"),
    workspace: str = typer.Option(".agintor_evo", "--workspace"),
) -> None:
    benchmark = load_suite(suite)
    provider_impl = _build_provider(provider, api_key_file)
    engine = EvolutionEngine(
        benchmark,
        Path(workspace),
        provider_impl,
        Path(runtime_dir),
        mutator_type=mutator,
        reference_runtime_dir=_reference_runtime_dir(provider, runtime_dir),
    )
    usage_before = provider_impl.usage_summary()
    summary = engine.run(steps=steps)
    typer.echo(json.dumps({**summary.__dict__, "provider_usage": _usage_delta(usage_before, provider_impl.usage_summary())}, indent=2, sort_keys=True))


@app.command("research")
def research_cmd(
    prompt: Optional[str] = typer.Argument(None),
    prompt_file: Optional[str] = typer.Option(None, "--prompt-file"),
    provider: str = typer.Option("openai", "--provider"),
    api_key_file: Optional[str] = typer.Option(None, "--api-key-file"),
    workspace: str = typer.Option(".agintor_research", "--workspace"),
    max_tracks: int = typer.Option(6, "--max-tracks"),
) -> None:
    prompt_text = _load_prompt_input(prompt, prompt_file)
    provider_impl = _build_provider(provider, api_key_file)
    if provider != "openai":
        raise typer.BadParameter("research currently requires --provider openai")
    if not isinstance(provider_impl, OpenAIProvider):
        raise typer.BadParameter("research currently requires an OpenAI provider")
    usage_before = provider_impl.usage_summary()
    try:
        result = run_research_prompt(prompt_text, provider_impl, Path(workspace), max_tracks=max_tracks)
    except AgintorError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        json.dumps(
            {
                "output_dir": result.output_dir,
                "answer_path": str(Path(result.output_dir) / "answer.md"),
                "json_path": str(Path(result.output_dir) / "research_run.json"),
                "source_count": len(result.unique_sources),
                "provider_usage": _usage_delta(usage_before, provider_impl.usage_summary()),
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    app()
