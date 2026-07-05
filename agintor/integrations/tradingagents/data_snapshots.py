from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ...utils import stable_hash, now_ts


class MarketDataSnapshot(BaseModel):
    snapshot_id: str
    as_of_ts: float
    vendor: str = "local"
    symbols: list[str] = Field(default_factory=list)
    prices: dict[str, Any] = Field(default_factory=dict)
    digest: str = ""

    def freeze(self) -> "MarketDataSnapshot":
        digest = stable_hash(self.snapshot_id, self.as_of_ts, self.vendor, self.symbols, self.prices)
        return self.model_copy(update={"digest": digest}, deep=True)


def write_snapshot(path: Path, snapshot: MarketDataSnapshot) -> MarketDataSnapshot:
    frozen = snapshot.freeze()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(frozen.model_dump(mode="json"), indent=2, sort_keys=True), encoding="utf-8")
    return frozen


def synthetic_post_close_snapshot(symbols: list[str]) -> MarketDataSnapshot:
    prices = {symbol: {"close": float(index + 1) * 100.0, "currency": "USD"} for index, symbol in enumerate(symbols)}
    return MarketDataSnapshot(snapshot_id=f"market-snapshot.{stable_hash(symbols)[:12]}", as_of_ts=now_ts(), vendor="synthetic", symbols=symbols, prices=prices).freeze()


__all__ = ["MarketDataSnapshot", "synthetic_post_close_snapshot", "write_snapshot"]
