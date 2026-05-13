from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class CompilerProposal:
    proposer_id: str
    proposal_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    rationale: str = ""
    confidence: float = 0.0


class OracleCompilerSubagent(Protocol):
    subagent_id: str

    def propose(self, state: dict[str, Any]) -> CompilerProposal: ...


@dataclass
class StaticSubagent:
    subagent_id: str
    proposal_type: str
    payload: dict[str, Any] = field(default_factory=dict)
    rationale: str = "static proposal"
    confidence: float = 0.5

    def propose(self, state: dict[str, Any]) -> CompilerProposal:
        return CompilerProposal(
            proposer_id=self.subagent_id,
            proposal_type=self.proposal_type,
            payload=dict(self.payload),
            rationale=self.rationale,
            confidence=self.confidence,
        )


__all__ = ["CompilerProposal", "OracleCompilerSubagent", "StaticSubagent"]
