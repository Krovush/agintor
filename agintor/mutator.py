from __future__ import annotations

import json
import random
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from .patches import build_patch, parse_patch
from .prompt_builder import build_mutation_prompt
from .providers import ModelProvider
from .runtime_loader import load_runtime
from .schemas import ModelRequest, MutationCandidate
from .utils import read_text, stable_hash, write_text


@dataclass
class MutationContext:
    objective: str
    touched_scope: list[str]
    runtime_dir: Path
    workspace: Path
    predictor_summaries: dict[str, object]
    failing_train_traces: list[dict[str, object]]
    exemplars: list[dict[str, object]]
    seed: int


class HeuristicPatchMutator:
    SCOPE_MUTATIONS = {
        "top": [
            ("topology_policy.py", "    SPAWN_PENALTY = 0.06", "    SPAWN_PENALTY = 0.03"),
            ("topology_policy.py", "    COORD_PENALTY = 0.05", "    COORD_PENALTY = 0.03"),
            ("topology_policy.py", "    THETA_CREATE = 0.58", "    THETA_CREATE = 0.52"),
        ],
        "mem": [
            ("memory_policy.py", "    THETA_PROM = 0.55", "    THETA_PROM = 0.48"),
            ("memory_policy.py", "    ETA_VERIFY = 0.50", "    ETA_VERIFY = 0.35"),
            ("memory_policy.py", "    B_HI = 0.75", "    B_HI = 0.68"),
        ],
        "tool": [
            ("tool_policy.py", "    BUILD_WEIGHT = 0.20", "    BUILD_WEIGHT = 0.12"),
            ("tool_policy.py", "    FUTURE_WEIGHT = 0.10", "    FUTURE_WEIGHT = 0.22"),
            ("tool_policy.py", "    ETA_R = 3", "    ETA_R = 2"),
        ],
        "ctl": [
            ("control_policy.py", '        "small": {"solve": 0.60, "cost": 0.10, "latency": 0.10, "dollar": 0.10, "fail": 0.12},', '        "small": {"solve": 0.64, "cost": 0.10, "latency": 0.10, "dollar": 0.10, "fail": 0.10},'),
            ("control_policy.py", '        "medium": {"solve": 0.74, "cost": 0.20, "latency": 0.16, "dollar": 0.18, "fail": 0.08},', '        "medium": {"solve": 0.78, "cost": 0.19, "latency": 0.15, "dollar": 0.17, "fail": 0.07},'),
        ],
    }

    def mutate(self, context: MutationContext) -> MutationCandidate:
        prompt = build_mutation_prompt(context.runtime_dir, context.objective, context.touched_scope, context.predictor_summaries, context.failing_train_traces, context.exemplars)
        runtime_name = f"cand_{stable_hash(context.runtime_dir, context.objective, context.touched_scope, context.seed)[:8]}"
        child_dir = context.workspace / runtime_name
        shutil.copytree(context.runtime_dir, child_dir)
        rng = random.Random(context.seed)
        blocks = []
        selected_scopes = list(context.touched_scope)
        rng.shuffle(selected_scopes)
        for scope in selected_scopes:
            choices = list(self.SCOPE_MUTATIONS.get(scope, []))
            if not choices:
                continue
            file_name, search, replace = choices[rng.randrange(len(choices))]
            path = child_dir / file_name
            source = path.read_text(encoding="utf-8")
            if search in source:
                path.write_text(source.replace(search, replace, 1), encoding="utf-8")
                blocks.append(build_patch(search, replace))
        if not blocks:
            file_name, search, replace = self.SCOPE_MUTATIONS[selected_scopes[0]][0]
            path = child_dir / file_name
            source = path.read_text(encoding="utf-8")
            path.write_text(source.replace(search, replace, 1), encoding="utf-8")
            blocks.append(build_patch(search, replace))
        return MutationCandidate(runtime_dir=str(child_dir), patch_text="\n".join(blocks), touched_scope=context.touched_scope, prompt=prompt, objective=context.objective)


class OpenAIPatchMutator:
    def __init__(self, provider: ModelProvider) -> None:
        self.provider = provider

    def mutate(self, context: MutationContext) -> MutationCandidate:
        prompt = build_mutation_prompt(context.runtime_dir, context.objective, context.touched_scope, context.predictor_summaries, context.failing_train_traces, context.exemplars)
        response = self.provider.generate(
            ModelRequest(
                instructions="Return only exact SEARCH/REPLACE blocks for the mutable Agintor runtime files.",
                prompt=prompt,
                model_class="large",
                seed=context.seed,
                metadata={"mode": "patch"},
            )
        )
        patch_text = response.text.strip()
        parse_patch(patch_text)
        runtime_name = f"cand_{stable_hash(context.runtime_dir, context.objective, context.touched_scope, context.seed)[:8]}"
        child_dir = context.workspace / runtime_name
        shutil.copytree(context.runtime_dir, child_dir)
        # patch application is handled by evaluator stage 0 to preserve exact checking.
        return MutationCandidate(runtime_dir=str(child_dir), patch_text=patch_text, touched_scope=context.touched_scope, prompt=prompt, objective=context.objective)
