from __future__ import annotations

from typing import Any, Mapping

from ...contracts import PredictorSnapshot


class RuntimePredictorState:
    def __init__(self, snapshot: Mapping[str, Any] | PredictorSnapshot | None = None) -> None:
        self._snapshot = PredictorSnapshot()
        if snapshot is not None:
            self.restore(snapshot)

    def snapshot(self) -> PredictorSnapshot:
        return self._snapshot.model_copy(deep=True)

    def restore(self, snapshot: Mapping[str, Any] | PredictorSnapshot) -> None:
        self._snapshot = (
            snapshot.model_copy(deep=True)
            if isinstance(snapshot, PredictorSnapshot)
            else PredictorSnapshot.model_validate(snapshot)
        )

    @classmethod
    def fork_from_snapshot(cls, snapshot: Mapping[str, Any] | PredictorSnapshot) -> "RuntimePredictorState":
        return cls(snapshot)
