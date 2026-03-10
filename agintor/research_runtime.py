from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .exceptions import AgintorError
from .prompts import load_prompt_spec
from .providers import OpenAIProvider
from .pydantic_compat import model_dump
from .research_models import (
    ExpandedQueries,
    ResearchArtifact,
    ResearchPlan,
    ResearchSourceRecord,
    ResearchTrackPlan,
    ResearchTrackReport,
)
from .schemas import AgentTemplate, BenchmarkTask, MemoryNode
from .utils import lexical_overlap, now_ts, stable_hash


_CITATION_PATTERN = re.compile(r"\[(S\d+)\]")


def build_research_task(
    prompt: str,
    *,
    task_id: str | None = None,
    family: str = "e2e",
    live_web: bool = True,
    max_tracks: int = 6,
    context_items: list[dict[str, Any]] | None = None,
    expected: Any | None = None,
    verifier_type: str = "research_report",
    min_source_count: int = 4,
    required_citation_count: int = 4,
    allow_best_effort: bool = False,
    proxy_scope_tags: list[str] | None = None,
    metadata: dict[str, Any] | None = None,
) -> BenchmarkTask:
    prompt_text = prompt.strip()
    if not prompt_text:
        raise AgintorError("research prompt may not be empty")
    task_metadata = dict(metadata or {})
    task_metadata.setdefault(
        "research",
        {
            "max_tracks": max(2, min(int(max_tracks), 8)),
            "mode": "live" if live_web else "frozen",
        },
    )
    return BenchmarkTask(
        task_id=task_id or f"research.{stable_hash(prompt_text)[:10]}",
        family=family,  # type: ignore[arg-type]
        prompt=prompt_text,
        task_type="research",
        context_items=list(context_items or []),
        operations=[],
        expected=expected
        or {
            "required_sections": ["Architecture", "Agent Topology", "Memory", "Tooling", "Evaluation"],
            "required_phrases": ["orchestrator", "subagent", "sources", "memory", "tooling", "evaluation"],
        },
        verifier_type=verifier_type,
        verification_required=True,
        allow_best_effort=allow_best_effort,
        min_source_count=min_source_count,
        required_citation_count=required_citation_count,
        live_web=live_web,
        frozen_corpus_id=None if live_web else "embedded",
        proxy_scope_tags=proxy_scope_tags or ["top", "mem", "tool", "ctl"],
        metadata=task_metadata,
    )


def run_runtime_research_task(runtime: Any, context: Any, frame: Any) -> dict[str, Any]:
    return model_dump(_RuntimeResearchAgent(runtime, context, frame).run())


class _RuntimeResearchAgent:
    def __init__(self, runtime: Any, context: Any, frame: Any) -> None:
        self.runtime = runtime
        self.context = context
        self.frame = frame
        research_meta = dict(context.task.metadata.get("research", {}))
        self.max_tracks = max(2, min(int(research_meta.get("max_tracks", 6)), 8))
        self.mode = "live" if bool(context.task.live_web) else str(research_meta.get("mode", "frozen"))

    def run(self) -> ResearchArtifact:
        prompt = self.context.task.prompt.strip()
        plan = self._plan(prompt)
        self.context.state.mode = "vertical" if len(plan.tracks) > 1 else "single"
        self.context.record("mode_selected", mode=self.context.state.mode)
        reports: list[ResearchTrackReport] = []
        source_meta: dict[str, ResearchSourceRecord] = {}
        for track in plan.tracks[: self.max_tracks]:
            report = self._run_track(prompt, track, source_meta)
            reports.append(report)
            self.context.state.artifacts[f"track:{track.track_id}"] = model_dump(report)
            self._remember_track(report)
        sources = self._build_source_registry(reports, source_meta)
        answer_markdown = self._synthesize(prompt, plan, reports, sources)
        cited_sources = self._filter_cited_sources(answer_markdown, sources)
        if cited_sources:
            sources = cited_sources
        artifact = ResearchArtifact(
            prompt=prompt,
            plan=plan,
            subagents=reports,
            sources=sources,
            answer_markdown=answer_markdown,
        )
        self._remember_answer(artifact)
        return artifact

    def _plan(self, prompt: str) -> ResearchPlan:
        if isinstance(self.context.provider, OpenAIProvider):
            spec = load_prompt_spec("runtime.root_decompose.v1")
            payload = {
                "user_prompt": prompt,
                "max_tracks": self.max_tracks,
                "mode": self.mode,
            }
            model_class = self._model_for("research_plan", "Plan research tracks", spec.model_class)
            response = self.context.provider.parse_response(
                text_format=ResearchPlan,
                model_class=model_class,
                instructions=spec.instructions,
                input=json.dumps(payload, indent=2),
                max_output_tokens=spec.max_output_tokens or 1800,
            )
            self._record_provider_response(response, model_class, spec.instructions, payload, "research_plan")
            parsed = getattr(response, "output_parsed", None)
            if isinstance(parsed, ResearchPlan) and parsed.tracks:
                return self._sanitize_plan(parsed, prompt)
        return self._heuristic_plan(prompt)

    def _sanitize_plan(self, parsed: ResearchPlan, prompt: str) -> ResearchPlan:
        tracks: list[ResearchTrackPlan] = []
        for index, track in enumerate(parsed.tracks[: self.max_tracks]):
            queries = [query.strip() for query in track.queries if str(query).strip()]
            if not queries:
                queries = [track.goal.strip() or f"Research track {index + 1}"]
            tracks.append(
                ResearchTrackPlan(
                    track_id=track.track_id or f"track_{index + 1}",
                    goal=track.goal.strip() or f"Research track {index + 1}",
                    queries=queries[:4],
                )
            )
        if not tracks:
            return self._heuristic_plan(prompt)
        return ResearchPlan(
            objective=parsed.objective.strip() or prompt,
            answer_outline=[item.strip() for item in parsed.answer_outline if str(item).strip()] or [track.goal for track in tracks],
            tracks=tracks,
        )

    def _heuristic_plan(self, prompt: str) -> ResearchPlan:
        sections = [
            "Architecture",
            "Agent Topology",
            "Memory",
            "Tooling and Containers",
            "Verification and Evolution",
            "Prompts and Policy",
        ]
        tracks: list[ResearchTrackPlan] = []
        for index, section in enumerate(sections[: self.max_tracks]):
            tracks.append(
                ResearchTrackPlan(
                    track_id=f"track_{index + 1}",
                    goal=section,
                    queries=[
                        f"{prompt} {section}",
                        f"{section} implementation details",
                        f"{section} constraints and tradeoffs",
                    ],
                )
            )
        return ResearchPlan(
            objective=prompt,
            answer_outline=[track.goal for track in tracks],
            tracks=tracks,
        )

    def _run_track(
        self,
        prompt: str,
        track: ResearchTrackPlan,
        source_meta: dict[str, ResearchSourceRecord],
    ) -> ResearchTrackReport:
        child_frame = type(self.frame)(
            agent=self._clone_agent("searcher"),
            objective=track.goal,
            operation_ids=[track.track_id],
            depth=self.frame.depth + 1,
            parent_id=self.frame.agent.agent_id,
            role="research_track",
            tool_scope=[],
            model_class=self._model_for("research_track", track.goal, "medium"),
        )
        run_node_id = self.context.shell.short_term.add_node(
            "AgentRun",
            track.track_id,
            {"goal": track.goal, "queries": list(track.queries)},
        )
        parent_run_node_id = self.frame.metadata.get("run_node_id")
        if parent_run_node_id and parent_run_node_id in self.context.shell.short_term.nodes:
            self.context.shell.short_term.add_edge(parent_run_node_id, run_node_id, "CALLS_AGENT")
        self.context.record("agent_start", agent_id=child_frame.agent.agent_id, role=child_frame.role, depth=child_frame.depth, op_ids=child_frame.operation_ids)
        queries = self._expand_queries(track)
        if self.mode == "live" and isinstance(self.context.provider, OpenAIProvider):
            report = self._research_track_live(prompt, track, queries, source_meta)
        else:
            report = self._research_track_frozen(prompt, track, queries, source_meta)
        checkpoint = self.runtime.topology.make_checkpoint(
            self.context,
            child_frame,
            {track.track_id: model_dump(report)},
            [],
            list(self.context.state.open_handle_ids),
        )
        self.context.state.checkpoints[track.track_id] = checkpoint
        self.context.record("child_complete", role="research_track", outputs=[track.track_id], sources=len(report.source_urls))
        self.context.record("agent_end", role="research_track", track_id=track.track_id)
        summary_node_id = self.context.shell.short_term.add_node("Summary", report.title, model_dump(checkpoint.summary), symbols=[track.track_id])
        self.context.shell.short_term.add_edge(run_node_id, summary_node_id, "EMITS")
        return report

    def _expand_queries(self, track: ResearchTrackPlan) -> list[str]:
        if isinstance(self.context.provider, OpenAIProvider):
            spec = load_prompt_spec("runtime.query_expand.v1")
            payload = {"track_goal": track.goal, "seed_queries": track.queries}
            model_class = self._model_for("research_queries", track.goal, spec.model_class)
            response = self.context.provider.parse_response(
                text_format=ExpandedQueries,
                model_class=model_class,
                instructions=spec.instructions,
                input=json.dumps(payload, indent=2),
                max_output_tokens=spec.max_output_tokens or 500,
            )
            self._record_provider_response(response, model_class, spec.instructions, payload, "research_queries")
            parsed = getattr(response, "output_parsed", None)
            if isinstance(parsed, ExpandedQueries):
                queries = [query.strip() for query in parsed.queries if str(query).strip()]
                if queries:
                    return self._dedupe_text(queries)[:4]
        return self._dedupe_text(track.queries)[:4]

    def _research_track_live(
        self,
        prompt: str,
        track: ResearchTrackPlan,
        queries: list[str],
        source_meta: dict[str, ResearchSourceRecord],
    ) -> ResearchTrackReport:
        spec = load_prompt_spec("runtime.source_extract.v1")
        payload = {
            "user_prompt": prompt,
            "track_id": track.track_id,
            "track_goal": track.goal,
            "search_queries": queries,
        }
        errors: list[str] = []
        model_class = self._model_for("research_search", track.goal, spec.model_class)
        for tool_type in ("web_search", "web_search_preview"):
            try:
                response = self.context.provider.parse_response(
                    text_format=ResearchTrackReport,
                    model_class=model_class,
                    instructions=spec.instructions,
                    input=json.dumps(payload, indent=2),
                    tools=[self._web_search_tool(tool_type)],
                    tool_choice={"type": tool_type},
                    include=["web_search_call.action.sources"],
                    max_tool_calls=8,
                    parallel_tool_calls=True,
                    max_output_tokens=spec.max_output_tokens or 1600,
                )
                self._record_provider_response(response, model_class, spec.instructions, payload, "research_search")
                parsed = getattr(response, "output_parsed", None)
                if not isinstance(parsed, ResearchTrackReport):
                    continue
                extracted = self._extract_source_records(response)
                for record in extracted:
                    source_meta[record.url] = record
                return ResearchTrackReport(
                    track_id=track.track_id,
                    title=parsed.title.strip() or track.goal,
                    summary=parsed.summary.strip(),
                    key_findings=[item.strip() for item in parsed.key_findings if str(item).strip()],
                    source_urls=self._dedupe_urls([*parsed.source_urls, *[record.url for record in extracted]]),
                )
            except Exception as exc:
                errors.append(f"{tool_type}: {exc}")
        raise AgintorError("web research request failed: " + " | ".join(errors))

    def _research_track_frozen(
        self,
        prompt: str,
        track: ResearchTrackPlan,
        queries: list[str],
        source_meta: dict[str, ResearchSourceRecord],
    ) -> ResearchTrackReport:
        docs = list(self.context.task.context_items)
        if not docs:
            return ResearchTrackReport(
                track_id=track.track_id,
                title=track.goal,
                summary=f"No frozen corpus was available for {track.goal}.",
                key_findings=["Frozen corpus missing for this research task."],
                source_urls=[],
            )
        scored: list[tuple[float, dict[str, Any]]] = []
        query_text = " ".join([prompt, track.goal, *queries])
        for doc in docs:
            title = str(doc.get("title", doc.get("doc_id", "")))
            content = str(doc.get("content", ""))
            tags = " ".join(str(tag) for tag in doc.get("tags", []))
            score = lexical_overlap(query_text, f"{title} {content} {tags}")
            score += 0.05 * sum(1 for query in queries if query.lower() in content.lower())
            scored.append((score, doc))
        selected = [doc for _, doc in sorted(scored, key=lambda item: (-item[0], str(item[1].get("doc_id", ""))))[:4]]
        source_urls: list[str] = []
        key_findings: list[str] = []
        for doc in selected:
            url = self._normalize_url(str(doc.get("url", f"frozen://{doc.get('doc_id', stable_hash(doc)[:8])}")))
            source_urls.append(url)
            title = str(doc.get("title", doc.get("doc_id", url)))
            snippet = self._snippet(str(doc.get("content", "")))
            key_findings.append(f"{title}: {snippet}")
            source_meta[url] = ResearchSourceRecord(
                source_id="",
                url=url,
                title=title,
                snippet=snippet,
                source_type="frozen",
                provenance={"doc_id": str(doc.get("doc_id", title))},
            )
        summary = f"{track.goal} is supported by {len(source_urls)} relevant frozen sources covering implementation details, constraints, and operational guidance."
        return ResearchTrackReport(
            track_id=track.track_id,
            title=track.goal,
            summary=summary,
            key_findings=key_findings[:6],
            source_urls=self._dedupe_urls(source_urls),
        )

    def _synthesize(
        self,
        prompt: str,
        plan: ResearchPlan,
        reports: list[ResearchTrackReport],
        sources: list[ResearchSourceRecord],
    ) -> str:
        if self.mode == "live" and isinstance(self.context.provider, OpenAIProvider):
            spec = load_prompt_spec("runtime.final_synthesis.v1")
            payload = {
                "user_prompt": prompt,
                "answer_outline": plan.answer_outline,
                "subagent_reports": [model_dump(report) for report in reports],
                "source_registry": [model_dump(source) for source in sources],
            }
            model_class = self._model_for("research_synthesis", "Synthesize research answer", spec.model_class)
            response = self.context.provider.create_response(
                model_class=model_class,
                instructions=spec.instructions,
                input=json.dumps(payload, indent=2),
                max_output_tokens=spec.max_output_tokens or 3200,
            )
            self._record_provider_response(response, model_class, spec.instructions, payload, "research_synthesis")
            answer = str(getattr(response, "output_text", "") or "").strip()
            if answer:
                return answer
        return self._synthesize_frozen(prompt, plan, reports, sources)

    def _synthesize_frozen(
        self,
        prompt: str,
        plan: ResearchPlan,
        reports: list[ResearchTrackReport],
        sources: list[ResearchSourceRecord],
    ) -> str:
        source_lookup = {source.url: source.source_id for source in sources}
        lines = ["# Research Answer", "", prompt, ""]
        for report in reports:
            lines.append(f"## {report.title}")
            report_source_ids = [source_lookup[url] for url in report.source_urls if url in source_lookup]
            citation_suffix = " ".join(f"[{source_id}]" for source_id in report_source_ids[:2])
            summary = report.summary.rstrip(".")
            lines.append(summary + (f" {citation_suffix}" if citation_suffix else ""))
            lines.append("")
            for index, finding in enumerate(report.key_findings):
                if report_source_ids:
                    source_id = report_source_ids[min(index, len(report_source_ids) - 1)]
                    lines.append(f"- {finding} [{source_id}]")
                else:
                    lines.append(f"- {finding}")
            lines.append("")
        lines.append("## Implementation Notes")
        lines.append("The runtime should preserve citations, checkpoints, verifier evidence, and delegated track summaries while synthesizing the final answer.")
        return "\n".join(lines).strip()

    def _remember_track(self, report: ResearchTrackReport) -> None:
        candidate = MemoryNode(
            node_id=stable_hash(self.context.task.task_id, report.track_id)[:16],
            type="TaskNote",
            label=report.title,
            content="\n".join([report.summary, *report.key_findings]).strip(),
            embedding=[],
            symbol_set=[report.track_id],
            file_paths=[],
            source_task_id=self.context.task.task_id,
            verifier_support=0.4,
            timestamps={"created": now_ts()},
            provenance={"source": "research_track"},
            tombstoned=False,
        )
        score = self.runtime.memory.score_memory_unit(self.context, candidate, self.context.shell.long_term.all_nodes())
        if self.runtime.memory.should_promote(self.context, candidate, score):
            action, target_id = self.runtime.memory.dedup_candidates(self.context, candidate, self.context.shell.long_term.all_nodes())
            self.runtime.memory.upsert_memory(self.context, candidate, action, target_id)

    def _remember_answer(self, artifact: ResearchArtifact) -> None:
        candidate = MemoryNode(
            node_id=stable_hash(self.context.task.task_id, artifact.answer_markdown)[:16],
            type="Answer",
            label=self.context.task.task_id,
            content=artifact.answer_markdown,
            embedding=[],
            symbol_set=list(self.context.task.symbolic_seeds),
            file_paths=[],
            source_task_id=self.context.task.task_id,
            verifier_support=1.0 if len(artifact.sources) >= self.context.task.min_source_count else 0.6,
            timestamps={"created": now_ts()},
            provenance={"source": "research_answer"},
            tombstoned=False,
        )
        action, target_id = self.runtime.memory.dedup_candidates(self.context, candidate, self.context.shell.long_term.all_nodes())
        self.runtime.memory.upsert_memory(self.context, candidate, action, target_id)

    def _build_source_registry(
        self,
        reports: list[ResearchTrackReport],
        source_meta: dict[str, ResearchSourceRecord],
    ) -> list[ResearchSourceRecord]:
        urls = self._dedupe_urls(url for report in reports for url in report.source_urls)
        sources: list[ResearchSourceRecord] = []
        for index, url in enumerate(urls):
            base = source_meta.get(url)
            sources.append(
                ResearchSourceRecord(
                    source_id=f"S{index + 1}",
                    url=url,
                    title="" if base is None else base.title,
                    snippet="" if base is None else base.snippet,
                    source_type="web" if base is None else base.source_type,
                    provenance={} if base is None else dict(base.provenance),
                )
            )
        return sources

    def _filter_cited_sources(
        self,
        answer_markdown: str,
        source_registry: list[ResearchSourceRecord],
    ) -> list[ResearchSourceRecord]:
        cited_ids = {match.group(1) for match in _CITATION_PATTERN.finditer(answer_markdown)}
        if not cited_ids:
            return source_registry[:40]
        return [row for row in source_registry if row.source_id in cited_ids]

    def _extract_source_records(self, response: Any) -> list[ResearchSourceRecord]:
        rows: list[ResearchSourceRecord] = []
        for item in getattr(response, "output", []) or []:
            if getattr(item, "type", None) != "web_search_call":
                continue
            action = getattr(item, "action", None)
            for source in getattr(action, "sources", None) or []:
                url = self._normalize_url(str(getattr(source, "url", "") or ""))
                if not url:
                    continue
                rows.append(
                    ResearchSourceRecord(
                        source_id="",
                        url=url,
                        title=str(getattr(source, "title", "") or ""),
                        snippet=str(getattr(source, "snippet", "") or ""),
                        source_type="web",
                        provenance={},
                    )
                )
        deduped: dict[str, ResearchSourceRecord] = {}
        for row in rows:
            deduped[row.url] = row
        return list(deduped.values())

    def _record_provider_response(
        self,
        response: Any,
        model_class: str,
        instructions: str,
        input_payload: Any,
        purpose: str,
    ) -> None:
        provider = self.context.provider
        if not isinstance(provider, OpenAIProvider):
            return
        recorded = provider._response_to_model_response(  # type: ignore[attr-defined]
            response=response,
            model_name=provider.resolve_model(model_class),
            prompt_text=f"{instructions}\n{json.dumps(input_payload, sort_keys=True, default=str)}",
        )
        self.context.consume_model_response(recorded, purpose=purpose)

    def _model_for(self, kind: str, description: str, default: str) -> str:
        operation = type(
            "ResearchOperation",
            (),
            {
                "kind": kind,
                "dependencies": [],
                "output_key": kind,
                "op_id": kind,
                "description": description,
            },
        )()
        try:
            return self.runtime.control.assign_model(self.context, operation, self.frame)
        except Exception:
            return default

    def _clone_agent(self, agent_id: str) -> AgentTemplate:
        try:
            return self.context.shell.agent_pool.clone(agent_id)
        except Exception:
            return self.context.shell.agent_pool.clone("root")

    def _snippet(self, text: str, limit: int = 220) -> str:
        compact = " ".join(text.split())
        return compact[:limit].rstrip() + ("..." if len(compact) > limit else "")

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

    def _dedupe_urls(self, urls: Iterable[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for raw_url in urls:
            url = self._normalize_url(str(raw_url or "").strip())
            if not url or url in seen:
                continue
            seen.add(url)
            ordered.append(url)
        return ordered

    def _dedupe_text(self, values: Iterable[str]) -> list[str]:
        seen: set[str] = set()
        ordered: list[str] = []
        for raw_value in values:
            value = " ".join(str(raw_value or "").split()).strip()
            if not value:
                continue
            key = value.lower()
            if key in seen:
                continue
            seen.add(key)
            ordered.append(value)
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
