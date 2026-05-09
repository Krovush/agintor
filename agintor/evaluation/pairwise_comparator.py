from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field


class AxisDuel(BaseModel):
    axis_id: str
    verdict: Literal["a_better", "b_better", "tie", "incomparable", "abstain"]
    margin: float = 0.0
    confidence: float = 0.0
    concrete_reasons: list[str] = Field(default_factory=list)
    checkable_subclaims: list[str] = Field(default_factory=list)


class PairwiseArtifactComparator:
    def __init__(self, scorer: Callable[[Any], float] | None = None, *, tie_margin: float = 0.02) -> None:
        self.scorer = scorer or self._default_score
        self.tie_margin = tie_margin

    def compare(self, *, axis_id: str, artifact_a: Any, artifact_b: Any) -> AxisDuel:
        try:
            score_a = float(self.scorer(artifact_a))
            score_b = float(self.scorer(artifact_b))
        except Exception:
            return AxisDuel(axis_id=axis_id, verdict="abstain", concrete_reasons=["scorer_failed"])
        margin = score_b - score_a
        if abs(margin) <= self.tie_margin:
            verdict = "tie"
        elif margin > 0:
            verdict = "b_better"
        else:
            verdict = "a_better"
        return AxisDuel(
            axis_id=axis_id,
            verdict=verdict,
            margin=abs(margin),
            confidence=min(1.0, abs(margin)),
            concrete_reasons=[f"score_a={score_a}", f"score_b={score_b}"],
        )

    @staticmethod
    def _default_score(artifact: Any) -> float:
        if isinstance(artifact, bool):
            return 1.0 if artifact else 0.0
        if isinstance(artifact, (int, float)):
            return float(artifact)
        if isinstance(artifact, dict) and "score" in artifact:
            return float(artifact["score"])
        raise TypeError("artifact has no deterministic score")


def decoded_winner(verdict: AxisDuel, *, artifact_a_id: str, artifact_b_id: str) -> str:
    if verdict.verdict == "a_better":
        return artifact_a_id
    if verdict.verdict == "b_better":
        return artifact_b_id
    return verdict.verdict
