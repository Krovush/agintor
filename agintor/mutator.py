from __future__ import annotations

import json
import random
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .patches import build_patch, parse_patch
from .prompts import load_prompt_spec
from .prompt_builder import build_mutation_prompt
from .providers import ModelProvider
from .runtime_profile import load_runtime_profile
from .schemas import ModelRequest, MutationCandidate


@dataclass
class MutationContext:
    objective: str
    touched_scope: list[str]
    runtime_dir: Path
    workspace: Path
    runtime_profile: object | None
    predictor_summaries: dict[str, object]
    failing_train_traces: list[dict[str, object]]
    exemplars: list[dict[str, object]]
    seed: int


class HeuristicPatchMutator:
    SCOPE_MUTATIONS = {
        "top": [
            (
                "topology_policy.py",
                "                solve = 0.62 + 0.06 * min(3, op_count) + 0.04 * dependency_count + 0.05 * exact_verifier_hint",
                "                solve = 0.66 + 0.05 * min(3, op_count) + 0.04 * dependency_count + 0.06 * exact_verifier_hint",
            ),
            (
                "topology_policy.py",
                "            delta = 0.52 + 0.10 * (op.kind in {\"generated_expression\", \"memory_lookup\"}) - self.SPAWN_PENALTY - self.COORD_PENALTY * min(2, index) - self.DEP_PENALTY * len(op.dependencies)",
                "            delta = 0.56 + 0.10 * (op.kind in {\"generated_expression\", \"memory_lookup\"}) - self.SPAWN_PENALTY - self.COORD_PENALTY * min(2, index) - 0.03 * len(op.dependencies)",
            ),
            (
                "topology_policy.py",
                "                score = solve_term - 0.12 * diversity_penalty - 0.06 * (len(selected) + 1)",
                "                score = solve_term - 0.10 * diversity_penalty - 0.05 * (len(selected) + 1)",
            ),
        ],
        "mem": [
            (
                "memory_policy.py",
                "            score = retained_utility + 0.04 * token_saving - 0.15 * info_loss - 0.05 * comp_latency - 0.20 * orphan_penalty",
                "            score = retained_utility + 0.05 * token_saving - 0.14 * info_loss - 0.05 * comp_latency - 0.18 * orphan_penalty",
            ),
            (
                "memory_policy.py",
                "        logits = 1.2 * novelty + 0.9 * reuse + 0.8 * centrality + 1.0 * verifier + 0.5 * task_spread + 0.6 * compositional - 1.0 * duplicate - 0.4 * write_cost - 0.8 * contradiction",
                "        logits = 1.3 * novelty + 0.9 * reuse + 0.8 * centrality + 1.1 * verifier + 0.5 * task_spread + 0.6 * compositional - 0.9 * duplicate - 0.4 * write_cost - 0.8 * contradiction",
            ),
            (
                "memory_policy.py",
                "                score = 0.30 * cos + 0.20 * lex + 0.15 * type_match + 0.10 * path_bonus + 0.10 * recency + 0.10 * verify + 0.05 * provenance - 0.05 * staleness",
                "                score = 0.28 * cos + 0.20 * lex + 0.15 * type_match + 0.14 * path_bonus + 0.08 * recency + 0.10 * verify + 0.05 * provenance - 0.05 * staleness",
            ),
        ],
        "tool": [
            (
                "tool_policy.py",
                "            score = 0.30 * sim + 0.20 * sigmatch + 0.15 * tool.pass_rate + 0.10 * cachehit - 0.10 * coldstart - 0.07 * permrisk - 0.08 * depdepth",
                "            score = 0.32 * sim + 0.22 * sigmatch + 0.15 * tool.pass_rate + 0.10 * cachehit - 0.09 * coldstart - 0.07 * permrisk - 0.07 * depdepth",
            ),
            (
                "tool_policy.py",
                "        best_reuse_gain = 0.0 if not ranked_reusable_tool_names else 0.55",
                "        best_reuse_gain = 0.0 if not ranked_reusable_tool_names else 0.50",
            ),
            (
                "tool_policy.py",
                "        current_gain = 0.85 if operation.kind == \"generated_expression\" else 0.20",
                "        current_gain = 0.88 if operation.kind == \"generated_expression\" else 0.20",
            ),
        ],
        "ctl": [
            (
                "control_policy.py",
                "        required = 0.60 + 0.10 * (operation.kind == \"generated_expression\") + 0.05 * bool(operation.dependencies)",
                "        required = 0.58 + 0.10 * (operation.kind == \"generated_expression\") + 0.05 * bool(operation.dependencies)",
            ),
            (
                "control_policy.py",
                "            utility = spec[\"solve\"] - 0.20 * spec[\"cost\"] - 0.15 * spec[\"latency\"] - 0.10 * spec[\"dollar\"] - 0.20 * spec[\"fail\"]",
                "            utility = spec[\"solve\"] - 0.18 * spec[\"cost\"] - 0.15 * spec[\"latency\"] - 0.10 * spec[\"dollar\"] - 0.18 * spec[\"fail\"]",
            ),
        ],
    }

    def mutate(self, context: MutationContext) -> MutationCandidate:
        prompt = build_mutation_prompt(
            context.runtime_dir,
            context.objective,
            context.touched_scope,
            context.predictor_summaries,
            context.failing_train_traces,
            context.exemplars,
            runtime_profile=context.runtime_profile,
        )
        rng = random.Random(context.seed)
        blocks: list[str] = []
        selected_scopes = list(context.touched_scope)
        rng.shuffle(selected_scopes)
        for scope in selected_scopes:
            viable_choices = []
            for file_name, search, replace in self.SCOPE_MUTATIONS.get(scope, []):
                source = (context.runtime_dir / file_name).read_text(encoding="utf-8")
                if search in source:
                    viable_choices.append((search, replace))
            if not viable_choices:
                continue
            search, replace = viable_choices[rng.randrange(len(viable_choices))]
            blocks.append(build_patch(search, replace))
        if not blocks:
            fallback_scope = selected_scopes[0]
            for file_name, search, replace in self.SCOPE_MUTATIONS[fallback_scope]:
                source = (context.runtime_dir / file_name).read_text(encoding="utf-8")
                if search in source:
                    blocks.append(build_patch(search, replace))
                    break
        return MutationCandidate(runtime_dir=str(context.runtime_dir), patch_text="\n".join(blocks), touched_scope=context.touched_scope, prompt=prompt, objective=context.objective)


class ProviderPatchMutator:
    def __init__(self, provider: ModelProvider) -> None:
        self.provider = provider

    @staticmethod
    def _coerce_patch_text(text: str) -> str:
        stripped = str(text or "").strip()
        if not stripped:
            return stripped
        try:
            payload = json.loads(stripped)
        except Exception:
            return stripped
        if isinstance(payload, dict):
            for key in ("patch_text", "patch", "response"):
                candidate = payload.get(key)
                if isinstance(candidate, str) and candidate.strip():
                    return candidate.strip()
        return stripped

    def _request_patch(self, *, instructions: str, prompt: str, model_class: str, seed: int, mode: str) -> str:
        response = self.provider.generate(
            ModelRequest(
                instructions=instructions,
                prompt=prompt,
                model_class=model_class,
                seed=seed,
                metadata={"mode": mode, "max_output_tokens": 16000},
            )
        )
        return self._coerce_patch_text(response.text)

    def mutate(self, context: MutationContext) -> MutationCandidate:
        prompt = build_mutation_prompt(
            context.runtime_dir,
            context.objective,
            context.touched_scope,
            context.predictor_summaries,
            context.failing_train_traces,
            context.exemplars,
            runtime_profile=context.runtime_profile,
        )
        profile = context.runtime_profile or load_runtime_profile(context.runtime_dir)
        spec = load_prompt_spec(profile.prompts.mutation_patch)
        patch_text = self._request_patch(
            instructions=spec.instructions,
            prompt=prompt,
            model_class=spec.model_class,
            seed=context.seed,
            mode="patch",
        )
        try:
            parse_patch(patch_text)
        except Exception:
            repair_instructions = (
                "Return only exact SEARCH/REPLACE blocks. "
                "Repair the candidate answer into valid SEARCH/REPLACE blocks against the original mutation prompt. "
                "Do not include prose or code fences."
            )
            repair_prompt = json.dumps(
                {
                    "original_prompt": prompt,
                    "candidate_answer": patch_text,
                },
                indent=2,
                sort_keys=True,
            )
            patch_text = self._request_patch(
                instructions=repair_instructions,
                prompt=repair_prompt,
                model_class=spec.model_class,
                seed=context.seed,
                mode="patch_repair",
            )
            parse_patch(patch_text)
        return MutationCandidate(runtime_dir=str(context.runtime_dir), patch_text=patch_text, touched_scope=context.touched_scope, prompt=prompt, objective=context.objective)


OpenAIPatchMutator = ProviderPatchMutator
