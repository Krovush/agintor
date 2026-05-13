from __future__ import annotations

from pydantic import BaseModel, Field

from ...utils import stable_hash


class MarketDataSnapshot(BaseModel):
    snapshot_id: str
    symbol: str
    as_of: str
    source: str
    rows: list[dict] = Field(default_factory=list)
    digest: str = ""

    def sealed_digest(self) -> str:
        return self.digest or stable_hash(self.model_dump(mode="json", exclude_none=True))


__all__ = ["MarketDataSnapshot"]
