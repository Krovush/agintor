from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from ..contracts import RuntimeManifest, RuntimeSpec, SpecAction, SpecMutationLedgerEntry, action_id_for, apply_spec_actions, validate_runtime_spec_payload
from ..runtime.profile import RUNTIME_PROFILE_FILE
from ..runtime.langgraph.compiler import RUNTIME_SPEC_FILE, RuntimeSpecCompiler
from ..utils import ensure_directory, stable_hash


@dataclass(frozen=True)
class SpecMutationContext:
    objective: str
    touched_scope: Sequence[str]
    runtime_dir: Path
    workspace: Path
    seed: int = 0
    predictor_summaries: dict[str, Any] = field(default_factory=dict)
    failing_train_traces: list[dict[str, Any]] = field(default_factory=list)
    exemplars: list[dict[str, Any]] = field(default_factory=list)
    oracle_package_hash: str = ""
    evidence_digest: str = ""


@dataclass(frozen=True)
class SpecMutationCandidate:
    child_runtime_dir: Path
    actions: list[SpecAction]
    parent_spec_digest: str
    child_spec_digest: str
    mutation_ledger_path: str


def load_runtime_spec_from_dir(runtime_dir: str | Path) -> RuntimeSpec:
    runtime_dir = Path(runtime_dir)
    return validate_runtime_spec_payload(json.loads((runtime_dir / RUNTIME_SPEC_FILE).read_text(encoding="utf-8")))


def write_runtime_spec_to_dir(runtime_dir: str | Path, spec: RuntimeSpec) -> None:
    runtime_dir = Path(runtime_dir)
    (runtime_dir / RUNTIME_SPEC_FILE).write_text(json.dumps(spec.model_dump(mode="json", exclude_none=True), indent=2, sort_keys=True), encoding="utf-8")


class HeuristicSpecActionMutator:
    """Typed v2 mutator that changes RuntimeSpec, never private validators."""

    def propose_actions(self, context: SpecMutationContext, parent_spec: RuntimeSpec) -> list[SpecAction]:
        scope = list(context.touched_scope or ["top"])
        action_seed = stable_hash(parent_spec.spec_digest, context.objective, scope, context.seed)
        if "ctl" in scope:
            return [
                SpecAction(
                    action_id=action_id_for("set_budget_policy", {"max_steps": parent_spec.execution.max_steps + 1}, seed=action_seed),
                    action_type="set_budget_policy",
                    scope=[item for item in scope if item in {"top", "mem", "tool", "ctl"}] or ["ctl"],
                    target_ids=[],
                    rationale="Increase graph-step budget for harder frontier tasks.",
                    expected_effect="May reduce premature failures while keeping host-side budget accounting.",
                    patch={"max_steps": min(parent_spec.execution.max_steps + 1, 32)},
                )
            ]
        if "tool" in scope and parent_spec.tools:
            tool = parent_spec.tools[0]
            return [
                SpecAction(
                    action_id=action_id_for("set_tool_policy", {"tool_id": tool.tool_id, "runtime_visible": True}, seed=action_seed),
                    action_type="set_tool_policy",
                    target_ids=[tool.tool_id],
                    scope=["tool"],
                    rationale="Keep useful tools explicitly runtime-visible in the typed spec.",
                    expected_effect="Improves tool availability without exposing sealed validators.",
                    patch={"runtime_visible": True},
                )
            ]
        root = parent_spec.agents[0] if parent_spec.agents else None
        if root is not None:
            return [
                SpecAction(
                    action_id=action_id_for("set_prompt", {"agent": root.agent_id, "objective": context.objective}, seed=action_seed),
                    action_type="set_prompt",
                    target_ids=[root.agent_id],
                    scope=["top" if "top" in scope else scope[0]],
                    rationale="Specialize root prompt to the selected objective and frozen oracle frontier.",
                    expected_effect="Improves output focus while preserving graph structure.",
                    patch={"output_instructions": f"Optimize for {context.objective}; cite observable evidence and avoid guessing sealed answers."},
                )
            ]
        return []

    def mutate(self, context: SpecMutationContext) -> SpecMutationCandidate:
        parent_spec = load_runtime_spec_from_dir(context.runtime_dir)
        actions = self.propose_actions(context, parent_spec)
        child_spec, results = apply_spec_actions(parent_spec, actions)
        child_dir = ensure_directory(Path(context.workspace) / f"spec_child_{stable_hash(parent_spec.spec_digest, [a.action_id for a in actions])[:10]}")
        if child_dir.exists():
            shutil.rmtree(child_dir)
        RuntimeSpecCompiler().compile_to_directory(child_spec, child_dir, force=True)
        profile_path = Path(context.runtime_dir) / RUNTIME_PROFILE_FILE
        if profile_path.is_file():
            shutil.copy2(profile_path, child_dir / RUNTIME_PROFILE_FILE)
        oracle_dir = Path(context.runtime_dir) / "oracle"
        if oracle_dir.is_dir():
            shutil.copytree(oracle_dir, child_dir / "oracle", dirs_exist_ok=True)
        manifest_path = child_dir / "runtime_manifest.json"
        manifest = RuntimeManifest.model_validate(json.loads(manifest_path.read_text(encoding="utf-8")))
        manifest = manifest.model_copy(
            update={
                "oracle_package_hash": context.oracle_package_hash,
                "metadata": {
                    **dict(manifest.metadata or {}),
                    "oracle_package_hash": context.oracle_package_hash,
                    "parent_spec_digest": parent_spec.spec_digest,
                    "mutation_action_ids": [action.action_id for action in actions],
                },
            },
            deep=True,
        )
        manifest_path.write_text(json.dumps(manifest.model_dump(mode="json", exclude_none=True), indent=2, sort_keys=True), encoding="utf-8")
        ledger_path = child_dir / "mutation_ledger.jsonl"
        entries = [
            SpecMutationLedgerEntry(
                action=action,
                result=result,
                oracle_package_hash=context.oracle_package_hash,
                evidence_digest=context.evidence_digest,
            )
            for action, result in zip(actions, results)
        ]
        from ..contracts import write_mutation_ledger

        write_mutation_ledger(ledger_path, entries)
        return SpecMutationCandidate(
            child_runtime_dir=child_dir,
            actions=actions,
            parent_spec_digest=parent_spec.spec_digest,
            child_spec_digest=child_spec.spec_digest,
            mutation_ledger_path=str(ledger_path),
        )


class ProviderSpecActionMutator(HeuristicSpecActionMutator):
    def __init__(self, provider: Any) -> None:
        self.provider = provider

    # Pass 1 keeps provider output advisory. The typed action validator is the authority.


__all__ = [
    "HeuristicSpecActionMutator",
    "ProviderSpecActionMutator",
    "SpecMutationCandidate",
    "SpecMutationContext",
    "load_runtime_spec_from_dir",
    "write_runtime_spec_to_dir",
]
