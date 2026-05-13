from __future__ import annotations

from pathlib import Path

from ...runtime.langgraph.compiler import RuntimeSpecCompiler
from .adapter import tradingagents_seed_spec
from .spec import TradingAgentsRuntimeSpec


class TradingAgentsCompiler:
    def compile_seed(self, destination: str | Path, *, symbols: list[str] | None = None, force: bool = False) -> TradingAgentsRuntimeSpec:
        spec = tradingagents_seed_spec(symbols=symbols)
        RuntimeSpecCompiler().compile_to_directory(spec, destination, force=force)
        return spec


__all__ = ["TradingAgentsCompiler"]
