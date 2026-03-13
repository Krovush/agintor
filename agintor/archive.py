from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .pydantic_compat import model_copy
from .schemas import ArchiveEntry, ArchiveRecord, ObjectiveKind, ObjectiveSpec, RuntimeDescriptor, SuiteEvaluation
from .utils import mean, normalize01, softmax, stable_hash


INTERFACES = ["top", "mem", "tool", "ctl"]
ALL_SCOPES = [
    ["top"], ["mem"], ["tool"], ["ctl"],
    ["top", "mem"], ["top", "tool"], ["top", "ctl"], ["mem", "tool"], ["mem", "ctl"], ["tool", "ctl"],
    ["top", "mem", "tool"], ["top", "mem", "ctl"], ["top", "tool", "ctl"], ["mem", "tool", "ctl"],
    ["top", "mem", "tool", "ctl"],
]
PHASE_SCOPES = {
    "local": [scope for scope in ALL_SCOPES if len(scope) == 1],
    "pair": [scope for scope in ALL_SCOPES if len(scope) == 2],
    "joint": [scope for scope in ALL_SCOPES if len(scope) >= 3],
}


def _ordered_scope(scope: Sequence[str]) -> tuple[str, ...]:
    order = {name: idx for idx, name in enumerate(INTERFACES)}
    return tuple(sorted(scope, key=lambda name: order[name]))



def objective_specs_from_suite(suite, partition: str = "train") -> list[ObjectiveSpec]:
    tasks = suite.all_tasks(partition)
    specs = [ObjectiveSpec(name=f"s:{task.task_id}", kind=ObjectiveKind.SINGLE_TASK, task_id=task.task_id, family=task.family) for task in tasks]
    for family in ["top", "mem", "tool", "e2e"]:
        specs.append(ObjectiveSpec(name=f"sbar:{family}", kind=ObjectiveKind.FAMILY, family=family))
        specs.append(ObjectiveSpec(name=f"rhobar:{family}", kind=ObjectiveKind.FAMILY_ROBUST, family=family))
    specs.append(ObjectiveSpec(name="sbar:global", kind=ObjectiveKind.GLOBAL))
    specs.append(ObjectiveSpec(name="rhobar:global", kind=ObjectiveKind.GLOBAL_ROBUST))
    return specs



def interface_bitmask(scope: Sequence[str]) -> str:
    active = set(scope)
    return "".join("1" if name in active else "0" for name in INTERFACES)



def behavior_descriptor(evaluation: SuiteEvaluation) -> list[str]:
    modes = [run.mode or "single" for run in evaluation.run_results]
    dominant_mode = max(sorted(set(modes)), key=lambda mode: modes.count(mode)) if modes else "single"
    created_rate = sum(run.created_tools for run in evaluation.run_results) / max(1, len(evaluation.run_results))
    promotion_density = sum(run.promoted_nodes for run in evaluation.run_results) / max(1, len(evaluation.run_results))
    checks_density = sum(run.checks_used for run in evaluation.run_results) / max(1, len(evaluation.run_results))

    def tri_bin(value: float, thresholds: tuple[float, float]) -> str:
        if value < thresholds[0]:
            return "low"
        if value < thresholds[1]:
            return "mid"
        return "high"

    return [dominant_mode, tri_bin(created_rate, (0.34, 0.67)), tri_bin(promotion_density, (0.34, 0.67)), tri_bin(checks_density, (0.75, 1.5))]


@dataclass
class ScopeScheduler:
    beta_scope: float = 2.0
    omega: tuple[float, float, float, float, float, float] = (0.40, 0.30, 0.10, 0.10, 0.05, 0.05)
    xi_f: float = 0.20
    xi_a: float = 0.20
    xi_b: float = 0.20
    phase: str = "local"
    a: dict[str, float] = field(default_factory=lambda: {name: 0.0 for name in INTERFACES})
    b: dict[tuple[str, str], float] = field(default_factory=lambda: {pair: 0.0 for pair in combinations(INTERFACES, 2)})
    c_f: dict[str, dict[tuple[str, ...], float]] = field(default_factory=lambda: defaultdict(dict))
    stagnation: dict[tuple[str, ...], float] = field(default_factory=lambda: {tuple(scope): 0.0 for scope in ALL_SCOPES})
    need: dict[tuple[str, ...], float] = field(default_factory=lambda: {tuple(scope): 0.0 for scope in ALL_SCOPES})
    hardfail: dict[tuple[str, ...], float] = field(default_factory=lambda: {tuple(scope): 0.0 for scope in ALL_SCOPES})

    def admissible_scopes(self) -> list[list[str]]:
        return [list(scope) for scope in PHASE_SCOPES[self.phase]]

    def aggregate_credit(self, scope: Sequence[str]) -> float:
        total = sum(self.a[item] for item in scope)
        for pair in combinations(_ordered_scope(scope), 2):
            total += self.b[pair]
        return total

    def utility(self, scope: Sequence[str], objective: str) -> float:
        key = _ordered_scope(scope)
        c_obj = self.c_f.get(objective, {}).get(key, 0.0)
        w1, w2, w3, w4, w5, w6 = self.omega
        return (
            w1 * self.aggregate_credit(scope)
            + w2 * c_obj
            + w3 * self.stagnation[key]
            + w4 * self.need[key]
            - w5 * self.hardfail[key]
            - w6 * len(scope)
        )

    def sample_scope(self, objective: str, seed: int) -> list[str]:
        import random

        scopes = self.admissible_scopes()
        scores = [self.utility(scope, objective) for scope in scopes]
        probs = softmax(scores, beta=self.beta_scope)
        rng = random.Random(seed)
        draw = rng.random()
        cumulative = 0.0
        for scope, prob in zip(scopes, probs):
            cumulative += prob
            if draw <= cumulative:
                return scope
        return scopes[-1]

    def update_scope_credit(self, objective: str, scope: Sequence[str], delta: float) -> None:
        key = _ordered_scope(scope)
        self.c_f.setdefault(objective, {})[key] = (1.0 - self.xi_f) * self.c_f.setdefault(objective, {}).get(key, 0.0) + self.xi_f * delta
        self.stagnation[key] = max(0.0, self.stagnation[key] * 0.9)

    def update_counterfactuals(self, scope: Sequence[str], singleton: Mapping[str, float], pairwise: Mapping[tuple[str, str], float]) -> None:
        for item, delta in singleton.items():
            self.a[item] = (1.0 - self.xi_a) * self.a[item] + self.xi_a * delta
        for pair, delta in pairwise.items():
            ordered = _ordered_scope(pair)
            self.b[ordered] = (1.0 - self.xi_b) * self.b[ordered] + self.xi_b * delta

    def note_hard_failure(self, scope: Sequence[str]) -> None:
        key = _ordered_scope(scope)
        self.hardfail[key] = min(1.0, 0.8 * self.hardfail[key] + 0.2)

    def note_iteration(self, accepted_scopes: Iterable[Sequence[str]]) -> None:
        accepted = {_ordered_scope(scope) for scope in accepted_scopes}
        for key in list(self.stagnation):
            if key not in accepted:
                self.stagnation[key] += 0.05

    def maybe_advance_phase(self, validation_improvement: float, coverage: float, pass_rate: float, epsilon_delta: float = 0.002, eta_cov: float = 0.60, eta_pass: float = 0.05) -> bool:
        if validation_improvement < epsilon_delta and coverage > eta_cov and pass_rate > eta_pass:
            if self.phase == "local":
                self.phase = "pair"
                return True
            if self.phase == "pair":
                self.phase = "joint"
                return True
        return False


class QualityDiversityArchive:
    def __init__(self, beta_sel: float = 2.5, delta_f: float = 0.002) -> None:
        self.beta_sel = beta_sel
        self.delta_f = delta_f
        self.cells: dict[tuple[str, str], ArchiveRecord] = {}
        self.by_objective: dict[str, list[ArchiveRecord]] = defaultdict(list)
        self.runtime_dirs: dict[str, str] = {}
        self.runtime_evaluations: dict[str, SuiteEvaluation] = {}
        self.runtime_descriptors: dict[str, RuntimeDescriptor] = {}

    def island(self, objective: str) -> list[ArchiveRecord]:
        return list(self.by_objective.get(objective, []))

    def _complexity_bucket(self, descriptor: RuntimeDescriptor) -> int:
        counts = [d.mutable_ast_nodes for d in self.runtime_descriptors.values()]
        if not counts:
            return 0
        counts_sorted = sorted(counts)
        quartiles = [counts_sorted[int((len(counts_sorted) - 1) * q)] for q in (0.25, 0.5, 0.75)]
        if descriptor.mutable_ast_nodes <= quartiles[0]:
            return 0
        if descriptor.mutable_ast_nodes <= quartiles[1]:
            return 1
        if descriptor.mutable_ast_nodes <= quartiles[2]:
            return 2
        return 3

    def descriptor(
        self,
        runtime_hash: str,
        code_hash: str,
        mutable_loc: int,
        evaluation: SuiteEvaluation,
        scope: Sequence[str],
        mutable_ast_nodes: int | None = None,
        interface_diff_mask: str | None = None,
    ) -> RuntimeDescriptor:
        ast_nodes = mutable_ast_nodes if mutable_ast_nodes is not None else mutable_loc
        descriptor = RuntimeDescriptor.from_runtime_hash(
            runtime_hash,
            behavior_descriptor(evaluation),
            "+".join(sorted(scope)) or "seed",
            0,
            mutable_loc,
            mutable_ast_nodes=ast_nodes,
            interface_diff_mask=interface_diff_mask or interface_bitmask(scope),
        )
        bucket = self._complexity_bucket(descriptor)
        return model_copy(descriptor, update={"complexity_bucket": bucket, "code_hash": code_hash})

    def _cell_key(self, objective: str, descriptor: RuntimeDescriptor, scope: Sequence[str]) -> str:
        return stable_hash(objective, descriptor.interface_diff_mask, descriptor.behavior_bin, descriptor.scope_tag, descriptor.complexity_bucket)

    def _trace_refs_for_objective(self, objective: str, evaluation: SuiteEvaluation) -> list[str]:
        if objective.startswith("s:"):
            task_id = objective.split(":", 1)[1]
            return [run.trace_path for run in evaluation.run_results if run.task_id == task_id]
        if ":" in objective:
            _, family = objective.split(":", 1)
            if family in {"top", "mem", "tool", "e2e"}:
                return [run.trace_path for run in evaluation.run_results if run.task_id.startswith(f"{family}.")]
        return [run.trace_path for run in evaluation.run_results]

    def insert(
        self,
        runtime_dir: str,
        runtime_hash: str,
        code_hash: str,
        mutable_loc: int,
        evaluation: SuiteEvaluation,
        scope: Sequence[str],
        mutable_ast_nodes: int | None = None,
        interface_diff_mask: str | None = None,
    ) -> list[str]:
        inserted = []
        descriptor = self.descriptor(
            runtime_hash,
            code_hash,
            mutable_loc,
            evaluation,
            scope,
            mutable_ast_nodes=mutable_ast_nodes,
            interface_diff_mask=interface_diff_mask,
        )
        self.runtime_dirs[runtime_hash] = runtime_dir
        self.runtime_evaluations[runtime_hash] = evaluation
        self.runtime_descriptors[runtime_hash] = descriptor
        for objective, score in evaluation.objective_scores.items():
            cell_key = self._cell_key(objective, descriptor, scope)
            entry = ArchiveEntry(
                code_hash=code_hash,
                runtime_hash=runtime_hash,
                scores=evaluation.objective_scores,
                behavior_bin=descriptor.behavior_bin,
                scope_tag=descriptor.scope_tag,
                complexity_bucket=descriptor.complexity_bucket,
                mutable_loc=mutable_loc,
                trace_refs=self._trace_refs_for_objective(objective, evaluation),
            )
            record = ArchiveRecord(objective=objective, key=cell_key, entry=entry, runtime_dir=runtime_dir)
            incumbent = self.cells.get((objective, cell_key))
            if incumbent is None:
                self.cells[(objective, cell_key)] = record
                self.by_objective[objective].append(record)
                inserted.append(cell_key)
                continue
            incumbent_score = incumbent.entry.scores.get(objective, float("-inf"))
            if score > incumbent_score + self.delta_f or (abs(score - incumbent_score) <= self.delta_f and mutable_loc < incumbent.entry.mutable_loc):
                self.cells[(objective, cell_key)] = record
                island = self.by_objective[objective]
                island[:] = [r for r in island if r.key != cell_key]
                island.append(record)
                inserted.append(cell_key)
        return inserted

    def select_parent(self, objective: str, seed: int):
        import random

        island = self.island(objective)
        if not island:
            raise ValueError(f"no island members for objective {objective}")
        scores = [record.entry.scores.get(objective, 0.0) for record in island]
        mu = mean(scores)
        sigma = math.sqrt(mean([(s - mu) ** 2 for s in scores])) + 1e-9
        normalized = [(s - mu) / sigma for s in scores]
        probs = softmax(normalized, beta=self.beta_sel)
        rng = random.Random(seed)
        draw = rng.random()
        cumulative = 0.0
        for record, prob in zip(island, probs):
            cumulative += prob
            if draw <= cumulative:
                return record
        return island[-1]
