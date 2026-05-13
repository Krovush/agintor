from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from ..utils import stable_hash


SubagentRole = Literal[
    "goal_analyst",
    "domain_analyst",
    "benchmark_designer",
    "validator_author",
    "fixture_author",
    "leakage_critic",
    "health_critic",
    "package_finalizer",
]


@dataclass(frozen=True)
class OracleSubagentProposal:
    proposal_id: str
    role: SubagentRole
    summary: str
    payload: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    risk_flags: list[str] = field(default_factory=list)


def make_subagent_proposal(role: SubagentRole, summary: str, payload: dict[str, Any] | None = None, *, confidence: float = 0.5, risk_flags: list[str] | None = None) -> OracleSubagentProposal:
    payload = dict(payload or {})
    return OracleSubagentProposal(
        proposal_id=f"oracle-subagent.{role}.{stable_hash(role, summary, payload)[:12]}",
        role=role,
        summary=summary,
        payload=payload,
        confidence=float(confidence),
        risk_flags=list(risk_flags or []),
    )


def leakage_critic_scan(public_payload: dict[str, Any], forbidden_keys: list[str]) -> OracleSubagentProposal:
    leaks: list[str] = []

    def walk(value: Any, path: str = "") -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                text = str(key)
                if text in forbidden_keys or text.startswith("private_"):
                    leaks.append(path + "." + text if path else text)
                walk(item, path + "." + text if path else text)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, f"{path}[{index}]")

    walk(public_payload)
    return make_subagent_proposal(
        "leakage_critic",
        "Public projection leakage scan completed.",
        {"leaks": leaks},
        confidence=1.0,
        risk_flags=["sealed_projection_leakage"] if leaks else [],
    )


__all__ = ["OracleSubagentProposal", "SubagentRole", "leakage_critic_scan", "make_subagent_proposal"]
