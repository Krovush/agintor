from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from ..contracts import ArchiveEntry, ArchiveRecord, ObjectiveKind, ObjectiveSpec, PromotionDecision, RuntimeDescriptor, SuiteEvaluation, decision_attr, decision_field_value
from ..utils import mean, softmax, stable_hash


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
ARCHIVE_KINDS = ("capability", "efficiency", "subskill", "preference")


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
        self.cells_by_kind: dict[str, dict[tuple[str, str], ArchiveRecord]] = {kind: {} for kind in ARCHIVE_KINDS}
        self.by_objective_by_kind: dict[str, defaultdict[str, list[ArchiveRecord]]] = {
            kind: defaultdict(list) for kind in ARCHIVE_KINDS
        }
        self.cells = self.cells_by_kind["capability"]
        self.by_objective = self.by_objective_by_kind["capability"]
        self.runtime_dirs: dict[str, str] = {}
        self.runtime_evaluations: dict[str, SuiteEvaluation] = {}
        self.runtime_descriptors: dict[str, RuntimeDescriptor] = {}

    def _stores(self, archive_kind: str) -> tuple[dict[tuple[str, str], ArchiveRecord], defaultdict[str, list[ArchiveRecord]]]:
        if archive_kind not in ARCHIVE_KINDS:
            raise ValueError(f"unknown archive kind {archive_kind!r}")
        return self.cells_by_kind[archive_kind], self.by_objective_by_kind[archive_kind]

    def archive_records(self, archive_kind: str | None = None) -> list[ArchiveRecord]:
        if archive_kind is not None:
            cells, _ = self._stores(archive_kind)
            return list(cells.values())
        records: list[ArchiveRecord] = []
        for cells in self.cells_by_kind.values():
            records.extend(cells.values())
        return records

    def island(self, objective: str, archive_kind: str | None = None) -> list[ArchiveRecord]:
        if archive_kind is None:
            archive_kind = "capability"
        _, by_objective = self._stores(archive_kind)
        return list(by_objective.get(objective, []))

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
        return (descriptor).model_copy(update={"complexity_bucket": bucket, "code_hash": code_hash})

    def _cell_key(self, archive_kind: str, objective: str, descriptor: RuntimeDescriptor, scope: Sequence[str]) -> str:
        return stable_hash(archive_kind, objective, descriptor.interface_diff_mask, descriptor.behavior_bin, descriptor.scope_tag, descriptor.complexity_bucket)

    def _trace_refs_for_objective(self, objective: str, evaluation: SuiteEvaluation) -> list[str]:
        if objective.startswith("s:"):
            task_id = objective.split(":", 1)[1]
            return [run.trace_ref() for run in evaluation.run_results if run.task_id == task_id]
        if ":" in objective:
            _, family = objective.split(":", 1)
            if family in {"top", "mem", "tool", "e2e"}:
                return [run.trace_ref() for run in evaluation.run_results if run.task_id.startswith(f"{family}.")]
        return [run.trace_ref() for run in evaluation.run_results]

    def _first_decision_float(self, decision: PromotionDecision | Mapping[str, Any] | None, names: Sequence[str]) -> float | None:
        for name in names:
            value = decision_attr(decision, name)
            if value is not None:
                return float(value)
        return None

    def _promotion_score(
        self,
        archive_kind: str,
        objective: str,
        evaluation: SuiteEvaluation,
        promotion_decision: PromotionDecision | Mapping[str, Any] | None,
    ) -> float:
        objective_score = float(evaluation.objective_scores.get(objective, 0.0))
        if archive_kind == "efficiency":
            efficiency_delta = self._first_decision_float(
                promotion_decision,
                ("efficiency_delta_lower", "efficiency_delta_estimate"),
            )
            return float(efficiency_delta) if efficiency_delta is not None else 0.0
        return objective_score

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
        *,
        archive_kind: str = "capability",
        promotion_decision: PromotionDecision | Mapping[str, Any] | None = None,
        objectives: Sequence[str] | None = None,
    ) -> list[str]:
        cells, by_objective = self._stores(archive_kind)
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
        target_objectives = list(objectives) if objectives is not None else list(evaluation.objective_scores)
        for objective in target_objectives:
            score = self._promotion_score(archive_kind, objective, evaluation, promotion_decision)
            cell_key = self._cell_key(archive_kind, objective, descriptor, scope)
            entry = ArchiveEntry(
                code_hash=code_hash,
                runtime_hash=runtime_hash,
                scores=evaluation.objective_scores,
                behavior_bin=descriptor.behavior_bin,
                scope_tag=descriptor.scope_tag,
                complexity_bucket=descriptor.complexity_bucket,
                mutable_loc=mutable_loc,
                trace_refs=self._trace_refs_for_objective(objective, evaluation),
                promotion_type=decision_attr(promotion_decision, "decision_type"),
                promotion_decision_ref=decision_attr(promotion_decision, "decision_id"),
                progress_signal_ref=decision_attr(promotion_decision, "progress_signal_ref"),
                evidence_contract_id=decision_field_value(promotion_decision, "contract_id"),
                evidence_digest=decision_field_value(promotion_decision, "evidence_digest"),
                promotion_score=score,
                improved_axes=list(decision_attr(decision_attr(promotion_decision, "progress_signal"), "improved_axes", []) or []),
                regressed_axes=list(decision_attr(decision_attr(promotion_decision, "progress_signal"), "regressed_axes", []) or []),
                tied_axes=list(decision_attr(decision_attr(promotion_decision, "progress_signal"), "tied_axes", []) or []),
            )
            record = ArchiveRecord(
                objective=objective,
                key=cell_key,
                entry=entry,
                runtime_dir=runtime_dir,
                archive_kind=archive_kind,
                promotion_type=decision_attr(promotion_decision, "decision_type"),
                promotion_decision_ref=decision_attr(promotion_decision, "decision_id"),
                evidence_contract_id=decision_field_value(promotion_decision, "contract_id"),
            )
            incumbent = cells.get((objective, cell_key))
            if incumbent is None:
                cells[(objective, cell_key)] = record
                by_objective[objective].append(record)
                inserted.append(cell_key)
                continue
            incumbent_score = incumbent.entry.promotion_score
            if incumbent_score is None:
                incumbent_score = incumbent.entry.scores.get(objective, float("-inf"))
            if score > incumbent_score + self.delta_f or (abs(score - incumbent_score) <= self.delta_f and mutable_loc < incumbent.entry.mutable_loc):
                cells[(objective, cell_key)] = record
                island = by_objective[objective]
                island[:] = [r for r in island if r.key != cell_key]
                island.append(record)
                inserted.append(cell_key)
        if inserted:
            self.runtime_dirs[runtime_hash] = runtime_dir
            self.runtime_evaluations[runtime_hash] = evaluation
            self.runtime_descriptors[runtime_hash] = descriptor
        return inserted

    def select_parent(
        self,
        objective: str,
        seed: int,
        archive_kind: str | None = None,
        archive_kinds: Sequence[str] | None = None,
    ):
        import random

        if archive_kinds is not None:
            island = []
            for kind in archive_kinds:
                island.extend(self.island(objective, archive_kind=kind))
        else:
            island = self.island(objective, archive_kind=archive_kind)
        if not island:
            raise ValueError(f"no island members for objective {objective}")
        scores = [
            record.entry.promotion_score
            if record.entry.promotion_score is not None
            else record.entry.scores.get(objective, 0.0)
            for record in island
        ]
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
