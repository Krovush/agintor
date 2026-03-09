from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, Field

from .exceptions import AgintorError
from .providers import OpenAIProvider
from .pydantic_compat import model_dump
from .utils import ensure_directory, stable_hash


_CITATION_PATTERN = re.compile(r"\[(S\d+)\]")


class ResearchTrackPlan(BaseModel):
    track_id: str
    goal: str
    queries: list[str] = Field(default_factory=list)


class ResearchPlan(BaseModel):
    objective: str
    answer_outline: list[str] = Field(default_factory=list)
    tracks: list[ResearchTrackPlan] = Field(default_factory=list)


class ResearchTrackReport(BaseModel):
    track_id: str
    title: str
    summary: str
    key_findings: list[str] = Field(default_factory=list)
    source_urls: list[str] = Field(default_factory=list)


class ResearchRun(BaseModel):
    prompt: str
    plan: ResearchPlan
    subagents: list[ResearchTrackReport]
    unique_sources: list[str]
    answer_markdown: str
    provider_usage: dict[str, Any]
    output_dir: str


class DeepResearchAgent:
    def __init__(
        self,
        provider: OpenAIProvider,
        workspace: str | Path,
        max_tracks: int = 6,
    ) -> None:
        self.provider = provider
        self.workspace = ensure_directory(Path(workspace))
        self.max_tracks = max(2, min(int(max_tracks), 8))

    def run(self, prompt: str) -> ResearchRun:
        clean_prompt = prompt.strip()
        if not clean_prompt:
            raise AgintorError("research prompt may not be empty")
        run_dir = ensure_directory(
            self.workspace / f"research_{int(time.time())}_{stable_hash(clean_prompt)[:8]}"
        )
        plan = self._plan(clean_prompt)
        reports = [self._research_track(clean_prompt, track) for track in plan.tracks[: self.max_tracks]]
        source_registry = self._build_source_registry(reports)
        answer_markdown = self._synthesize(clean_prompt, plan, reports, source_registry)
        cited_registry = self._filter_cited_sources(answer_markdown, source_registry)
        if cited_registry:
            source_registry = cited_registry
        payload = ResearchRun(
            prompt=clean_prompt,
            plan=plan,
            subagents=reports,
            unique_sources=[row["url"] for row in source_registry],
            answer_markdown=answer_markdown,
            provider_usage=self.provider.usage_summary(),
            output_dir=str(run_dir),
        )
        self._write_outputs(run_dir, payload, source_registry)
        return payload

    def _plan(self, prompt: str) -> ResearchPlan:
        instructions = (
            "You are Agintor's research planner. Break the user's request into focused research lanes "
            "that can be handled by independent search subagents. Prefer implementation-relevant lanes "
            "covering architecture, prompt design, tool stack, memory, safety/policy, containers/runtime "
            "isolation, evaluation, and deployment concerns."
        )
        response = self.provider.parse_response(
            text_format=ResearchPlan,
            model_class="large",
            instructions=instructions,
            input=json.dumps(
                {
                    "user_prompt": prompt,
                    "max_tracks": self.max_tracks,
                    "requirements": [
                        "Each track must be materially different.",
                        "Each track must contain 2-4 concrete web search queries.",
                        "Answer outline should be implementation-oriented.",
                    ],
                },
                indent=2,
            ),
            max_output_tokens=1800,
        )
        parsed = getattr(response, "output_parsed", None)
        if not isinstance(parsed, ResearchPlan):
            raise AgintorError("planner did not return a valid research plan")
        tracks: list[ResearchTrackPlan] = []
        for index, track in enumerate(parsed.tracks[: self.max_tracks]):
            queries = [query.strip() for query in track.queries if query.strip()]
            if not queries:
                queries = [track.goal]
            tracks.append(
                ResearchTrackPlan(
                    track_id=track.track_id or f"track_{index + 1}",
                    goal=track.goal.strip() or f"Research track {index + 1}",
                    queries=queries[:4],
                )
            )
        if not tracks:
            raise AgintorError("planner returned no research tracks")
        return ResearchPlan(
            objective=parsed.objective.strip() or prompt,
            answer_outline=[item.strip() for item in parsed.answer_outline if item.strip()],
            tracks=tracks,
        )

    def _research_track(self, prompt: str, track: ResearchTrackPlan) -> ResearchTrackReport:
        instructions = (
            "You are a delegated search subagent. Use web search to investigate only your assigned lane. "
            "Return a concise synthesis of what matters for implementing the user's requested system. "
            "Prefer practical architectural details, concrete techniques, and operational constraints."
        )
        search_input = {
            "user_prompt": prompt,
            "track_id": track.track_id,
            "track_goal": track.goal,
            "search_queries": track.queries,
            "required_shape": {
                "title": "short descriptive title",
                "summary": "one concise paragraph",
                "key_findings": ["4-8 implementation-relevant findings"],
                "source_urls": ["URLs only"],
            },
        }
        response = self._parse_with_web_search(
            text_format=ResearchTrackReport,
            model_class="medium",
            instructions=instructions,
            input=json.dumps(search_input, indent=2),
            max_output_tokens=1600,
        )
        parsed = getattr(response, "output_parsed", None)
        if not isinstance(parsed, ResearchTrackReport):
            raise AgintorError(f"research subagent {track.track_id} did not return a valid report")
        extracted_urls = self._extract_source_urls(response)
        merged_urls = self._dedupe_urls([*parsed.source_urls, *extracted_urls])
        return ResearchTrackReport(
            track_id=track.track_id,
            title=parsed.title.strip() or track.goal,
            summary=parsed.summary.strip(),
            key_findings=[item.strip() for item in parsed.key_findings if item.strip()],
            source_urls=merged_urls,
        )

    def _parse_with_web_search(
        self,
        *,
        text_format: type[BaseModel],
        model_class: str,
        instructions: str,
        input: str,
        max_output_tokens: int,
    ) -> Any:
        errors: list[str] = []
        for tool_type in ("web_search", "web_search_preview"):
            try:
                return self.provider.parse_response(
                    text_format=text_format,
                    model_class=model_class,
                    instructions=instructions,
                    input=input,
                    tools=[self._web_search_tool(tool_type)],
                    tool_choice={"type": tool_type},
                    include=["web_search_call.action.sources"],
                    max_tool_calls=8,
                    parallel_tool_calls=True,
                    max_output_tokens=max_output_tokens,
                )
            except Exception as exc:
                errors.append(f"{tool_type}: {exc}")
        raise AgintorError("web research request failed: " + " | ".join(errors))

    def _synthesize(
        self,
        prompt: str,
        plan: ResearchPlan,
        reports: list[ResearchTrackReport],
        source_registry: list[dict[str, str]],
    ) -> str:
        source_lookup = {row["url"]: row["id"] for row in source_registry}
        synthesis_input = {
            "user_prompt": prompt,
            "answer_outline": plan.answer_outline,
            "subagent_reports": [
                {
                    "track_id": report.track_id,
                    "title": report.title,
                    "summary": report.summary,
                    "key_findings": report.key_findings,
                    "source_ids": [source_lookup[url] for url in report.source_urls if url in source_lookup],
                }
                for report in reports
            ],
            "source_registry": source_registry,
        }
        instructions = (
            "You are the central research orchestrator. Combine the subagent findings into a thorough, "
            "implementation-oriented answer to the user's prompt. Write in Markdown. Cite concrete claims "
            "inline with the provided source ids like [S1]. Do not cite ids that are not in the source registry. "
            "If the user asks to create a system, provide an actionable design and missing implementation pieces."
        )
        response = self.provider.create_response(
            model_class="large",
            instructions=instructions,
            input=json.dumps(synthesis_input, indent=2),
            max_output_tokens=3200,
        )
        answer = str(getattr(response, "output_text", "") or "").strip()
        if not answer:
            raise AgintorError("final synthesis returned no answer text")
        return answer

    def _build_source_registry(self, reports: list[ResearchTrackReport]) -> list[dict[str, str]]:
        urls = self._dedupe_urls(url for report in reports for url in report.source_urls)
        return [{"id": f"S{index + 1}", "url": url} for index, url in enumerate(urls)]

    def _filter_cited_sources(self, answer_markdown: str, source_registry: list[dict[str, str]]) -> list[dict[str, str]]:
        cited_ids = {match.group(1) for match in _CITATION_PATTERN.finditer(answer_markdown)}
        if not cited_ids:
            return source_registry[:40]
        return [row for row in source_registry if row["id"] in cited_ids]

    def _extract_source_urls(self, response: Any) -> list[str]:
        urls: list[str] = []
        for item in getattr(response, "output", []) or []:
            if getattr(item, "type", None) != "web_search_call":
                continue
            action = getattr(item, "action", None)
            sources = getattr(action, "sources", None) or []
            for source in sources:
                url = getattr(source, "url", None)
                if isinstance(url, str) and url.strip():
                    urls.append(url.strip())
        return self._dedupe_urls(urls)

    def _write_outputs(
        self,
        run_dir: Path,
        payload: ResearchRun,
        source_registry: list[dict[str, str]],
    ) -> None:
        answer_path = run_dir / "answer.md"
        sources_block = "\n".join(f"- {row['id']}: {row['url']}" for row in source_registry)
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
            json.dumps(source_registry, indent=2, sort_keys=True),
            encoding="utf-8",
        )

    def _web_search_tool(self, tool_type: str) -> dict[str, Any]:
        return {
            "type": tool_type,
            "search_context_size": "high",
            "user_location": {
                "type": "approximate",
                "country": "US",
                "timezone": "America/New_York",
            },
        }

    def _dedupe_urls(self, urls: Any) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for raw_url in urls:
            url = self._normalize_url(str(raw_url or "").strip())
            if not url or url in seen:
                continue
            seen.add(url)
            ordered.append(url)
        return ordered

    def _normalize_url(self, url: str) -> str:
        if not url:
            return ""
        try:
            parts = urlsplit(url)
        except Exception:
            return url
        filtered_query = [
            (key, value)
            for key, value in parse_qsl(parts.query, keep_blank_values=True)
            if not key.lower().startswith("utm_") and key.lower() not in {"fbclid", "gclid"}
        ]
        path = parts.path[:-1] if parts.path.endswith("/") and parts.path != "/" else parts.path
        normalized = urlunsplit(
            (
                parts.scheme.lower(),
                parts.netloc.lower(),
                path,
                urlencode(filtered_query, doseq=True),
                "",
            )
        )
        return normalized or url


def run_research_prompt(
    prompt: str,
    provider: OpenAIProvider,
    workspace: str | Path,
    max_tracks: int = 6,
) -> ResearchRun:
    return DeepResearchAgent(provider=provider, workspace=workspace, max_tracks=max_tracks).run(prompt)
