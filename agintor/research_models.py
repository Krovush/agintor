from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ExpandedQueries(BaseModel):
    queries: list[str] = Field(default_factory=list)


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


class ResearchSourceRecord(BaseModel):
    source_id: str
    url: str
    title: str = ""
    snippet: str = ""
    source_type: str = "web"
    provenance: dict[str, Any] = Field(default_factory=dict)


class ResearchArtifact(BaseModel):
    prompt: str
    plan: ResearchPlan
    subagents: list[ResearchTrackReport]
    sources: list[ResearchSourceRecord]
    answer_markdown: str


class ResearchRun(BaseModel):
    prompt: str
    plan: ResearchPlan
    subagents: list[ResearchTrackReport]
    sources: list[ResearchSourceRecord] = Field(default_factory=list)
    unique_sources: list[str] = Field(default_factory=list)
    answer_markdown: str
    provider_usage: dict[str, Any]
    output_dir: str
    runtime_result: dict[str, Any] = Field(default_factory=dict)
