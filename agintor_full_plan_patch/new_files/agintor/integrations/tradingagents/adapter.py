from __future__ import annotations

from pathlib import Path
from typing import Any

from ...runtime.langgraph.compiler import LangGraphRuntimeCompiler
from .spec import TradingAgentsRuntimeSpec


class TradingAgentsAdapter:
    def compile_runtime(self, spec: TradingAgentsRuntimeSpec, output_dir: str | Path) -> Path:
        return LangGraphRuntimeCompiler().export_generated_app(spec, output_dir)

    def public_runtime_summary(self, spec: TradingAgentsRuntimeSpec) -> dict[str, Any]:
        return {
            "runtime_id": spec.runtime_id,
            "runtime_kind": spec.runtime_kind,
            "selected_analysts": list(spec.selected_analysts),
            "debate_rounds": spec.debate_rounds,
            "risk_discussion_rounds": spec.risk_discussion_rounds,
            "risk_policy_id": spec.risk_policy_id,
        }


__all__ = ["TradingAgentsAdapter"]
