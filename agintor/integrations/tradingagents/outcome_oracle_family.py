from __future__ import annotations

from ...oracle.families.trading_outcome import family as _trading_outcome_family


def family():
    return _trading_outcome_family()

__all__ = ["family"]
