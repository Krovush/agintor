from __future__ import annotations

from pathlib import Path

from agintor.contracts import LongTermGraphSnapshot, PredictorSnapshot, SolveResult


def _runtime_dir(tmp_path: Path, name: str = "runtime.alpha") -> Path:
    runtime_dir = tmp_path / name
    runtime_dir.mkdir(parents=True, exist_ok=True)
    return runtime_dir


def _solve_result_with_state(
    *,
    long_term_graph: LongTermGraphSnapshot | None = None,
    predictor_snapshot: PredictorSnapshot | None = None,
    short_term_export: list[dict] | None = None,
) -> SolveResult:
    return SolveResult(
        request_id="req.1",
        runtime_hash="runtime.hash.alpha",
        run_id="",
        attempt_id="",
        run_root="",
        run_lifecycle_state="completed",
        artifact={"text": "ok"},
        status="completed",
        summary="ok",
        post_message_long_term_graph=long_term_graph,
        post_message_predictor_snapshot=predictor_snapshot,
        post_message_short_term_export=list(short_term_export or []),
    )
