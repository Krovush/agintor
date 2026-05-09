from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..contracts import SuiteEvaluation, decision_attr, decision_type_value


@dataclass
class PredictorObservation:
    family: str
    feature_vector: list[float]
    label_probability: float | None = None
    label_positive_scalar: float | None = None
    metadata: dict[str, object] | None = None


def _event_counts(trace: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in trace:
        event = str(row.get("event", ""))
        counts[event] = counts.get(event, 0) + 1
    return counts


def _feature_vector(run, counts: dict[str, int], trace: list[dict[str, Any]], family_bias: float) -> list[float]:
    return [
        family_bias,
        float(len(trace)),
        float(counts.get("child_complete", 0)),
        float(counts.get("tool_operation", 0)),
        float(counts.get("checks_requested", 0)),
        float(counts.get("compaction", 0)),
        float(run.created_tools),
        float(run.checks_used),
        float(run.model_calls or 0),
    ]


def _base_metadata(
    run,
    *,
    accepted: bool | None,
    task_family: str,
    promotion_decision: Mapping[str, object] | object | None,
    decision_type: str,
    updates_capability_prior: bool,
) -> dict[str, object]:
    return {
        "accepted": accepted,
        "promotion_type": decision_type,
        "promotion_decision_type": decision_type,
        "promotion_decision_id": decision_attr(promotion_decision, "decision_id"),
        "promotion_contract_id": decision_attr(promotion_decision, "contract_id", ""),
        "promotion_comparison_ref": decision_attr(promotion_decision, "comparison_ref", ""),
        "updates_capability_prior": updates_capability_prior,
        "task_family": task_family,
        "task_id": run.task_id,
        "trace_path": run.trace_path,
        "trace_ref": run.trace_ref(),
    }


def extract_predictor_observations(
    evaluation: SuiteEvaluation,
    task_family_map: dict[str, str],
    *,
    promotion_decision: Mapping[str, object] | object | None = None,
    retained: bool | None = None,
) -> list[PredictorObservation]:
    observations: list[PredictorObservation] = []
    decision_type = decision_type_value(promotion_decision) or "abstain"
    accepted = retained if retained is not None else decision_type in {"capability", "efficiency", "subskill", "preference"}
    updates_capability_prior = decision_type == "capability"
    for run in evaluation.run_results:
        trace = run.trace_rows()
        counts = _event_counts(trace)
        family_name = task_family_map.get(run.task_id, "")
        family_bias = float(["top", "mem", "tool", "e2e"].index(family_name)) if family_name in {"top", "mem", "tool", "e2e"} else 0.0
        features = _feature_vector(run, counts, trace, family_bias)
        fault = 1.0 if run.faults > 0 or run.hard_invalid else 0.0
        cost = max(0.0, float(run.cost))
        latency = max(0.0, float(run.latency))
        tokens = max(1.0, float(run.tokens_used or (run.input_tokens + run.output_tokens) or 1.0))
        families: list[str] = []
        if counts.get("mode_selected", 0) or counts.get("child_complete", 0):
            families.append("topology")
        if counts.get("compaction", 0):
            families.append("compaction")
        if counts.get("tool_operation", 0) or run.created_tools:
            families.append("tooling")
        if counts.get("checks_requested", 0):
            families.append("verification")
        if counts.get("model_assigned", 0) or counts.get("model_response", 0):
            families.append("model")
        if counts.get("stop", 0):
            families.append("stopping")
        for family in families:
            metadata = _base_metadata(
                run,
                accepted=accepted,
                task_family=family_name,
                promotion_decision=promotion_decision,
                decision_type=decision_type,
                updates_capability_prior=updates_capability_prior,
            )
            if decision_type == "efficiency":
                observations.append(
                    PredictorObservation(
                        family=f"efficiency:{family}:cost",
                        feature_vector=features,
                        label_positive_scalar=cost,
                        metadata=metadata,
                    )
                )
                observations.append(
                    PredictorObservation(
                        family=f"efficiency:{family}:latency",
                        feature_vector=features,
                        label_positive_scalar=latency,
                        metadata=metadata,
                    )
                )
                observations.append(
                    PredictorObservation(
                        family=f"efficiency:{family}:token",
                        feature_vector=features,
                        label_positive_scalar=tokens,
                        metadata=metadata,
                    )
                )
                continue
            if decision_type == "capability":
                observations.append(
                    PredictorObservation(
                        family=f"capability:{family}",
                        feature_vector=features,
                        label_probability=1.0,
                        metadata=metadata,
                    )
                )
            elif decision_type in {"subskill", "preference", "reject", "no_progress", "abstain", "quarantine"}:
                observations.append(
                    PredictorObservation(
                        family=f"{decision_type}:{family}",
                        feature_vector=features,
                        label_probability=1.0,
                        metadata=metadata,
                    )
                )
                if decision_type not in {"reject", "quarantine"}:
                    continue
            observations.append(
                PredictorObservation(
                    family=f"{family}:fault",
                    feature_vector=features,
                    label_probability=fault,
                    metadata=metadata,
                )
            )
            observations.append(
                PredictorObservation(
                    family=f"{family}:cost",
                    feature_vector=features,
                    label_positive_scalar=cost,
                    metadata=metadata,
                )
            )
            observations.append(
                PredictorObservation(
                    family=f"{family}:latency",
                    feature_vector=features,
                    label_positive_scalar=latency,
                    metadata=metadata,
                )
            )
            observations.append(
                PredictorObservation(
                    family=f"{family}:token",
                    feature_vector=features,
                    label_positive_scalar=tokens,
                    metadata=metadata,
                )
            )
    return observations


def extract_predictor_observations_for_promotion(
    evaluation: SuiteEvaluation,
    task_family_map: dict[str, str],
    *,
    decision: Mapping[str, object] | object | None,
) -> list[PredictorObservation]:
    return extract_predictor_observations(
        evaluation,
        task_family_map,
        promotion_decision=decision,
    )
