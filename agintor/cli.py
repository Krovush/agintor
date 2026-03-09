from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from .benchmarks import load_suite
from .evaluator import RuntimeEvaluator
from .evolution import EvolutionEngine
from .project import init_runtime as init_runtime_dir, write_demo_suite
from .pydantic_compat import model_dump
from .providers import build_provider
from .runtime_loader import load_runtime


app = typer.Typer(add_completion=False, help="Agintor CLI MVP")



def _parse_seeds(text: str) -> list[int]:
    return [int(part.strip()) for part in text.split(",") if part.strip()]


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
    workspace: str = typer.Option(".agintor_runs", "--workspace"),
) -> None:
    benchmark = load_suite(suite)
    task = benchmark.by_id(task_id)
    runtime = load_runtime(runtime_dir)
    evaluator = RuntimeEvaluator(benchmark, Path(workspace), build_provider(provider), baseline_runtime_dir=Path(runtime_dir))
    evaluation = evaluator.evaluate_runtime(runtime_dir, partition=partition, seeds=[seed], use_cache=False, tasks_override=[task])
    typer.echo(json.dumps({
        "runtime_hash": runtime.runtime_hash,
        "task_id": task_id,
        "result": model_dump(evaluation.run_results[0]),
        "objective_scores": evaluation.objective_scores,
    }, indent=2, sort_keys=True))


@app.command("eval")
def eval_cmd(
    runtime_dir: str,
    suite: str = typer.Option("demo", "--suite"),
    partition: str = typer.Option("train", "--partition"),
    seeds: str = typer.Option("0,1,2", "--seeds"),
    provider: str = typer.Option("local", "--provider"),
    workspace: str = typer.Option(".agintor_runs", "--workspace"),
) -> None:
    benchmark = load_suite(suite)
    evaluator = RuntimeEvaluator(benchmark, Path(workspace), build_provider(provider), baseline_runtime_dir=Path(runtime_dir))
    evaluation = evaluator.evaluate_runtime(runtime_dir, partition=partition, seeds=_parse_seeds(seeds), use_cache=False)
    typer.echo(json.dumps(model_dump(evaluation), indent=2, sort_keys=True))


@app.command("evolve")
def evolve_cmd(
    runtime_dir: str,
    suite: str = typer.Option("demo", "--suite"),
    steps: int = typer.Option(10, "--steps"),
    provider: str = typer.Option("local", "--provider"),
    mutator: str = typer.Option("heuristic", "--mutator"),
    workspace: str = typer.Option(".agintor_evo", "--workspace"),
) -> None:
    benchmark = load_suite(suite)
    engine = EvolutionEngine(benchmark, Path(workspace), build_provider(provider), Path(runtime_dir), mutator_type=mutator)
    summary = engine.run(steps=steps)
    typer.echo(json.dumps(summary.__dict__, indent=2, sort_keys=True))


if __name__ == "__main__":
    app()
