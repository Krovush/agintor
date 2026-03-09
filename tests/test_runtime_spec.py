from __future__ import annotations

import json
from pathlib import Path

from agintor.benchmarks import build_demo_suite
from agintor.evaluator import RuntimeEvaluator
from agintor.providers import LocalDeterministicProvider
from agintor.schemas import ModelResponse
from agintor.verifiers import verify_task_with_evidence


def test_trace_verifier_supports_event_presence() -> None:
    task = build_demo_suite().by_id("proxy.tool.generated_trace")
    score, evidence = verify_task_with_evidence(
        task,
        artifact={"value": 2},
        trace=[
            {"event": "tool_operation"},
            {"event": "checks_requested"},
            {"event": "stop"},
        ],
    )
    assert score == 1.0
    assert evidence["matched"] is True


def test_trace_verifier_supports_event_counts() -> None:
    task = build_demo_suite().by_id("proxy.top.checkpoint_trace")
    score, evidence = verify_task_with_evidence(
        task,
        artifact={"sum": 10, "product": 30},
        trace=[
            {"event": "child_complete"},
            {"event": "child_complete"},
            {"event": "stop"},
        ],
    )
    assert score == 1.0
    assert evidence["observed"] >= 2


def test_demo_trace_proxies_pass(runtime_dir: Path, provider_local, tmp_path: Path) -> None:
    suite = build_demo_suite()
    evaluator = RuntimeEvaluator(suite, tmp_path / "eval", provider_local, baseline_runtime_dir=runtime_dir)
    proxy_tasks = [
        suite.by_id("proxy.top.checkpoint_trace"),
        suite.by_id("proxy.tool.generated_trace"),
        suite.by_id("proxy.tool.provider_synthesis"),
        suite.by_id("proxy.mem.compaction_trace"),
    ]
    evaluation = evaluator.evaluate_runtime(runtime_dir, partition="proxy", seeds=[0], use_cache=False, tasks_override=proxy_tasks)
    assert evaluation.invalid is False
    assert all(run.verifier_score == 1.0 for run in evaluation.run_results)


def test_provider_backed_local_synthesis_returns_useful_output(runtime_dir: Path, provider_local, tmp_path: Path) -> None:
    suite = build_demo_suite()
    task = suite.by_id("proxy.tool.provider_synthesis")
    evaluator = RuntimeEvaluator(suite, tmp_path / "provider_eval", provider_local, baseline_runtime_dir=None)
    run = evaluator.evaluate_runtime(runtime_dir, partition="proxy", seeds=[0], use_cache=False, tasks_override=[task]).run_results[0]
    trace = json.loads(Path(run.trace_path).read_text(encoding="utf-8"))
    assert run.hard_invalid is False
    assert run.artifact == 7
    assert "model_response" in [row.get("event") for row in trace]
    assert "tool_operation" in [row.get("event") for row in trace]


def test_provider_synthesis_falls_back_from_bad_model_output(runtime_dir: Path, tmp_path: Path) -> None:
    class BrokenToolSpecProvider(LocalDeterministicProvider):
        def generate(self, request):
            if request.metadata.get("mode") == "tool_spec":
                response = ModelResponse(
                    text='```json\n{"expression": "x + 1"}\n```',
                    raw={"provider": "test"},
                    model_name="test/tool-spec",
                    input_tokens=1,
                    output_tokens=1,
                    token_estimate=2,
                    latency_s=0.0,
                    dollar_cost=0.0,
                )
                self._record_usage(response)
                return response
            return super().generate(request)

    suite = build_demo_suite()
    task = suite.by_id("proxy.tool.provider_synthesis")
    evaluator = RuntimeEvaluator(suite, tmp_path / "eval", BrokenToolSpecProvider(), baseline_runtime_dir=None)
    run = evaluator.evaluate_runtime(runtime_dir, partition="proxy", seeds=[0], use_cache=False, tasks_override=[task]).run_results[0]
    assert run.hard_invalid is False
    assert run.artifact == 7


def test_compaction_proxy_limits_summary_calls(runtime_dir: Path, provider_local, tmp_path: Path) -> None:
    suite = build_demo_suite()
    task = suite.by_id("proxy.mem.compaction_trace")
    evaluator = RuntimeEvaluator(suite, tmp_path / "eval_compaction", provider_local, baseline_runtime_dir=None)
    run = evaluator.evaluate_runtime(runtime_dir, partition="proxy", seeds=[0], use_cache=False, tasks_override=[task]).run_results[0]
    trace = json.loads(Path(run.trace_path).read_text(encoding="utf-8"))
    assert run.hard_invalid is False
    assert run.verifier_score == 1.0
    assert any(row.get("event") == "compaction" for row in trace)
    assert run.artifact == "17"
