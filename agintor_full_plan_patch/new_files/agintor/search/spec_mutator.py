from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..contracts import RuntimeSpec, SpecAction, SpecActionBatch, apply_spec_actions, action_ledger_rows
from ..providers import ModelProvider, ModelRequest
from ..runtime.langgraph.compiler import LangGraphRuntimeCompiler
from ..utils import ensure_directory, stable_hash


@dataclass
class SpecMutationContext:
    objective: str
    touched_scope: list[str]
    runtime_spec: RuntimeSpec
    workspace: Path
    predictor_summaries: dict[str, object]
    failing_train_traces: list[dict[str, object]]
    exemplars: list[dict[str, object]]
    seed: int


@dataclass
class SpecMutationResult:
    parent_spec: RuntimeSpec
    child_spec: RuntimeSpec
    actions: list[SpecAction]
    action_batch: SpecActionBatch
    mutation_ledger_path: str
    compiled_runtime_dir: str = ""


class HeuristicSpecActionMutator:
    def mutate(self, context: SpecMutationContext) -> list[SpecAction]:
        rng = random.Random(context.seed)
        actions: list[SpecAction] = []
        if "ctl" in context.touched_scope:
            actions.append(
                SpecAction(
                    action_id=f"spec-action.{stable_hash(context.runtime_spec.spec_digest, context.seed, 'budget')[:12]}",
                    action_type="set_budget_policy",
                    target_ids=["execution"],
                    scope=[scope for scope in context.touched_scope if scope in {"top", "mem", "tool", "ctl"}],
                    rationale="Adjust runtime step budget for search exploration.",
                    expected_effect="Explore whether a slightly larger execution envelope improves objective performance.",
                    patch={"max_steps": max(4, int(context.runtime_spec.execution.max_steps) + rng.choice([-1, 1, 2]))},
                )
            )
        elif context.runtime_spec.agents:
            agent = context.runtime_spec.agents[rng.randrange(len(context.runtime_spec.agents))]
            actions.append(
                SpecAction(
                    action_id=f"spec-action.{stable_hash(context.runtime_spec.spec_digest, context.seed, agent.agent_id)[:12]}",
                    action_type="set_prompt",
                    target_ids=[agent.agent_id],
                    scope=[scope for scope in context.touched_scope if scope in {"top", "mem", "tool", "ctl"}] or ["top"],
                    rationale="Inject objective-specific bounded instruction into agent prompt.",
                    expected_effect="Improve behavior without changing private validation access.",
                    patch={"prompt": f"{agent.prompt}\n\nOptimization focus: {context.objective}. Use only runtime-visible evidence.".strip()},
                )
            )
        return actions


class ProviderSpecActionMutator:
    def __init__(self, provider: ModelProvider) -> None:
        self.provider = provider

    def mutate(self, context: SpecMutationContext) -> list[SpecAction]:
        request = ModelRequest(
            instructions=(
                "Return strict JSON with key actions. Each action must match the Agintor SpecAction schema. "
                "Do not add private, sealed, hidden, or oracle-only data to the runtime spec."
            ),
            prompt=json.dumps(
                {
                    "objective": context.objective,
                    "touched_scope": context.touched_scope,
                    "runtime_spec": context.runtime_spec.model_dump(mode="json", exclude_none=True),
                    "predictor_summaries": context.predictor_summaries,
                    "failing_train_traces": context.failing_train_traces[:4],
                    "exemplars": context.exemplars[:4],
                },
                indent=2,
                sort_keys=True,
            ),
            model_class="large",
            seed=context.seed,
            metadata={"mode": "spec_action_mutation", "max_output_tokens": 8000},
        )
        response = self.provider.generate(request)
        payload = json.loads(response.text)
        return [SpecAction.model_validate(item) for item in payload.get("actions", [])]


class SpecActionMutator:
    def __init__(self, provider: ModelProvider | None = None, *, use_provider: bool = False) -> None:
        self.inner = ProviderSpecActionMutator(provider) if use_provider and provider is not None else HeuristicSpecActionMutator()

    def mutate_and_compile(self, context: SpecMutationContext, *, compile_runtime: bool = True) -> SpecMutationResult:
        actions = self.inner.mutate(context)
        application = apply_spec_actions(context.runtime_spec, actions)
        child_payload = context.runtime_spec.model_dump(mode="json", exclude_none=True)
        # Rebuild child from application refs by reapplying actions so callers get a concrete spec.
        child = context.runtime_spec
        for action in actions:
            child = apply_spec_actions(child, [action]).model_copy if False else child
        from ..contracts.spec_actions import _apply_one  # local import keeps private helper out of public API
        child = context.runtime_spec
        for action in actions:
            child = _apply_one(child, action)
        child = child.model_copy(update={
            "parent_spec_digest": application.parent_spec_digest,
            "mutation_history": [*context.runtime_spec.mutation_history, *application.mutation_refs],
        }, deep=True)
        workspace = ensure_directory(context.workspace)
        ledger_path = workspace / "mutation_ledger.jsonl"
        with ledger_path.open("a", encoding="utf-8") as handle:
            for row in action_ledger_rows(application):
                handle.write(json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n")
        compiled_runtime_dir = ""
        if compile_runtime:
            runtime_dir = ensure_directory(workspace / f"runtime_{child.spec_digest[:12]}")
            LangGraphRuntimeCompiler().export_generated_app(child, runtime_dir)
            compiled_runtime_dir = str(runtime_dir)
        return SpecMutationResult(
            parent_spec=context.runtime_spec,
            child_spec=child,
            actions=actions,
            action_batch=SpecActionBatch(
                batch_id=f"spec-batch.{stable_hash(context.runtime_spec.spec_digest, [a.action_id for a in actions])[:12]}",
                parent_spec_digest=context.runtime_spec.spec_digest,
                actions=actions,
            ),
            mutation_ledger_path=str(ledger_path),
            compiled_runtime_dir=compiled_runtime_dir,
        )


__all__ = [
    "HeuristicSpecActionMutator",
    "ProviderSpecActionMutator",
    "SpecActionMutator",
    "SpecMutationContext",
    "SpecMutationResult",
]
