from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .container_runtime import DockerRuntimeExecutor
from .exceptions import AgintorError
from .project import baseline_template_dir
from .providers import OpenAIProvider
from .pydantic_compat import model_dump
from .research_models import (
    ExpandedQueries,
    ResearchArtifact,
    ResearchPlan,
    ResearchRun,
    ResearchSourceRecord,
    ResearchTrackPlan,
    ResearchTrackReport,
)
from .research_runtime import build_research_task
from .runner import TaskRuntime
from .runtime_loader import load_runtime
from .shell import FixedShell
from .utils import ensure_directory, stable_hash


class DeepResearchAgent:
    def __init__(
        self,
        provider: OpenAIProvider,
        workspace: str | Path,
        max_tracks: int = 6,
        runtime_dir: str | Path | None = None,
        containerized: bool | None = None,
    ) -> None:
        self.provider = provider
        self.workspace = ensure_directory(Path(workspace))
        self.max_tracks = max(2, min(int(max_tracks), 8))
        self.runtime_dir = Path(runtime_dir) if runtime_dir is not None else baseline_template_dir()
        self.containerized = (os.environ.get("AGINTOR_RESEARCH_CONTAINER", "1") != "0") if containerized is None else bool(containerized)
        self.container_executor = DockerRuntimeExecutor(self.workspace / ".container_cache")

    def run(self, prompt: str) -> ResearchRun:
        clean_prompt = prompt.strip()
        if not clean_prompt:
            raise AgintorError("research prompt may not be empty")
        run_dir = ensure_directory(
            self.workspace / f"research_{int(time.time())}_{stable_hash(clean_prompt)[:8]}"
        )
        task = build_research_task(
            clean_prompt,
            task_id=f"research.live.{stable_hash(clean_prompt)[:10]}",
            family="e2e",
            live_web=True,
            max_tracks=self.max_tracks,
            min_source_count=8,
            required_citation_count=8,
            allow_best_effort=True,
            metadata={"research": {"max_tracks": self.max_tracks, "mode": "live"}},
        )
        runtime = load_runtime(self.runtime_dir)
        if self.containerized:
            runtime_result = self.container_executor.run_task(
                self.runtime_dir,
                task,
                0,
                provider_name="openai",
                api_key_file=self.provider.api_key_file,
            )
        else:
            shell = FixedShell(run_dir / "runtime")
            runner = TaskRuntime(runtime, shell, self.provider)
            runtime_result = runner.run_task(task, 0)
        if runtime_result.hard_invalid:
            raise AgintorError(runtime_result.invalid_reason or "runtime-backed research run invalid")
        artifact_payload = runtime_result.artifact
        if not isinstance(artifact_payload, dict):
            raise AgintorError("runtime-backed research returned an invalid artifact payload")
        artifact = ResearchArtifact.parse_obj(artifact_payload)
        provider_usage = self.provider.usage_summary()
        if self.containerized:
            provider_usage = {
                "calls": int(runtime_result.model_calls),
                "input_tokens": int(runtime_result.input_tokens),
                "output_tokens": int(runtime_result.output_tokens),
                "total_tokens": int(runtime_result.tokens_used),
                "dollar_cost": float(runtime_result.cost),
            }
        payload = ResearchRun(
            prompt=artifact.prompt,
            plan=artifact.plan,
            subagents=artifact.subagents,
            sources=artifact.sources,
            unique_sources=[source.url for source in artifact.sources],
            answer_markdown=artifact.answer_markdown,
            provider_usage=provider_usage,
            output_dir=str(run_dir),
            runtime_result=model_dump(runtime_result),
        )
        self._write_outputs(run_dir, payload)
        return payload

    def _write_outputs(self, run_dir: Path, payload: ResearchRun) -> None:
        answer_path = run_dir / "answer.md"
        sources_block = "\n".join(f"- {row.source_id}: {row.url}" for row in payload.sources)
        answer_path.write_text(
            payload.answer_markdown.rstrip()
            + ("\n\n## Sources\n" + sources_block if sources_block else ""),
            encoding="utf-8",
        )
        (run_dir / "research_run.json").write_text(
            json.dumps(model_dump(payload), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        (run_dir / "source_registry.json").write_text(
            json.dumps([model_dump(source) for source in payload.sources], indent=2, sort_keys=True),
            encoding="utf-8",
        )


def run_research_prompt(
    prompt: str,
    provider: OpenAIProvider,
    workspace: str | Path,
    max_tracks: int = 6,
    runtime_dir: str | Path | None = None,
    containerized: bool | None = None,
) -> ResearchRun:
    return DeepResearchAgent(
        provider=provider,
        workspace=workspace,
        max_tracks=max_tracks,
        runtime_dir=runtime_dir,
        containerized=containerized,
    ).run(prompt)


__all__ = [
    "DeepResearchAgent",
    "ExpandedQueries",
    "ResearchArtifact",
    "ResearchPlan",
    "ResearchRun",
    "ResearchSourceRecord",
    "ResearchTrackPlan",
    "ResearchTrackReport",
    "run_research_prompt",
]
