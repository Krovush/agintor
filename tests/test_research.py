from __future__ import annotations

import json
from pathlib import Path

from typer.testing import CliRunner

from agintor.cli import app
from agintor.providers import load_openai_api_key_from_file, resolve_openai_api_key
from agintor.research import ResearchPlan, ResearchRun, ResearchTrackPlan, ResearchTrackReport


runner = CliRunner()


def test_load_openai_api_key_from_file_supports_assignment_format(tmp_path: Path) -> None:
    path = tmp_path / "OpenAI API Key.txt"
    path.write_text('OPENAI_API_KEY="sk-test-1234567890abcdefghijklmnop"\n', encoding="utf-8")
    assert load_openai_api_key_from_file(path) == "sk-test-1234567890abcdefghijklmnop"


def test_resolve_openai_api_key_uses_explicit_file_before_env_file(tmp_path: Path, monkeypatch) -> None:
    explicit = tmp_path / "explicit.txt"
    fallback = tmp_path / "fallback.txt"
    explicit.write_text("sk-explicit-1234567890abcdefghijklmnop", encoding="utf-8")
    fallback.write_text("sk-fallback-1234567890abcdefghijklmnop", encoding="utf-8")
    monkeypatch.setenv("AGINTOR_OPENAI_KEY_FILE", str(fallback))
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert resolve_openai_api_key(api_key_file=explicit) == "sk-explicit-1234567890abcdefghijklmnop"


def test_cli_research_accepts_prompt_file_and_api_key_file(tmp_path: Path, monkeypatch) -> None:
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("Create a deep research websearch agent.", encoding="utf-8")
    key_file = tmp_path / "key.txt"
    key_file.write_text("sk-test-1234567890abcdefghijklmnop", encoding="utf-8")
    output_dir = tmp_path / "research_output"

    def fake_run(prompt: str, provider, workspace: Path, max_tracks: int) -> ResearchRun:
        assert prompt == "Create a deep research websearch agent."
        assert max_tracks == 4
        provider._usage["calls"] = 3
        provider._usage["input_tokens"] = 120
        provider._usage["output_tokens"] = 80
        provider._usage["total_tokens"] = 200
        provider._usage["dollar_cost"] = 0.12
        workspace.mkdir(parents=True, exist_ok=True)
        return ResearchRun(
            prompt=prompt,
            plan=ResearchPlan(
                objective=prompt,
                answer_outline=["Architecture", "Tools"],
                tracks=[ResearchTrackPlan(track_id="track_1", goal="Architecture", queries=["agent orchestration"])],
            ),
            subagents=[
                ResearchTrackReport(
                    track_id="track_1",
                    title="Architecture",
                    summary="Use an orchestrator with delegated search workers.",
                    key_findings=["Fan out searches", "Normalize evidence"],
                    source_urls=["https://example.com/research"],
                )
            ],
            unique_sources=["https://example.com/research"],
            answer_markdown="# Answer",
            provider_usage=provider.usage_summary(),
            output_dir=str(output_dir),
        )

    monkeypatch.setattr("agintor.cli.run_research_prompt", fake_run)
    result = runner.invoke(
        app,
        [
            "research",
            "--prompt-file",
            str(prompt_file),
            "--api-key-file",
            str(key_file),
            "--workspace",
            str(tmp_path / "workspace"),
            "--max-tracks",
            "4",
        ],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["output_dir"] == str(output_dir)
    assert payload["source_count"] == 1
    assert payload["provider_usage"]["calls"] == 3
