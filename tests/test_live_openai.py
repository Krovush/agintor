from __future__ import annotations

import os
from pathlib import Path

import pytest

from agintor.benchmarks import build_demo_suite
from agintor.evaluator import RuntimeEvaluator
from agintor.providers import OpenAIProvider
from agintor.project import init_runtime
from agintor.schemas import ModelRequest

pytestmark = pytest.mark.usefixtures("module_failure_artifact_bucket")


@pytest.mark.live_openai
def test_openai_provider_live_roundtrip_with_mock_credentials() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("requires OPENAI_API_KEY")
    provider = OpenAIProvider(api_key=os.environ.get("OPENAI_API_KEY", "sk-mock"))
    response = provider.generate(
        ModelRequest(
            instructions="Respond with the word pong.",
            prompt="ping",
            model_class="small",
            seed=0,
            metadata={"mode": "text"},
        )
    )
    assert "pong" in response.text.strip().lower()


@pytest.mark.live_openai
def test_openai_runtime_live_compaction_proxy(tmp_path: Path) -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("requires OPENAI_API_KEY")
    if os.environ.get("AGINTOR_RUN_LIVE_RUNTIME") != "1":
        pytest.skip("set AGINTOR_RUN_LIVE_RUNTIME=1 to opt into paid runtime execution")
    provider = OpenAIProvider(api_key=os.environ.get("OPENAI_API_KEY"))
    runtime_dir = init_runtime(tmp_path / "runtime")
    suite = build_demo_suite()
    task = suite.by_id("proxy.mem.compaction_trace")
    evaluator = RuntimeEvaluator(suite, tmp_path / "eval", provider, baseline_runtime_dir=None)
    evaluation = evaluator.evaluate_runtime(runtime_dir, partition="proxy", seeds=[0], use_cache=False, tasks_override=[task])
    run = evaluation.run_results[0]
    assert run.hard_invalid is False
    assert run.verifier_score == 1.0
    assert run.model_calls >= 1
    assert run.tokens_used > 0
