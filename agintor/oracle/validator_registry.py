from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from ..contracts import ValidatorResult, ValidatorSpec
from ..utils import stable_hash

RunValidator = Callable[[ValidatorSpec, dict[str, Any]], ValidatorResult]
ApplicabilityFn = Callable[[dict[str, Any]], float]


@dataclass(frozen=True)
class ValidatorFamily:
    family_id: str
    description: str
    authority_ceiling: str = "A4"
    default_visibility: str = "sealed"
    leakage_risk: str = "medium"
    default_failure_action: str = "abstain"
    health_tests: tuple[str, ...] = ("positive_control", "negative_control")
    input_contract: dict[str, Any] = field(default_factory=dict)
    output_schema: dict[str, Any] = field(default_factory=dict)
    run: RunValidator | None = None
    applicability: ApplicabilityFn | None = None

    def score_applicability(self, context: dict[str, Any]) -> float:
        if self.applicability is None:
            return 0.0
        return float(self.applicability(context))

    def make_spec(self, *, validator_id: str, claim_ids: list[str], inputs: dict[str, Any] | None = None, visibility: str | None = None) -> ValidatorSpec:
        return ValidatorSpec(
            validator_id=validator_id,
            family_id=self.family_id,
            claim_ids=claim_ids,
            inputs=dict(inputs or {}),
            outputs_schema=dict(self.output_schema),
            authority_ceiling=self.authority_ceiling,
            visibility=visibility or self.default_visibility,
            independence_group=self.family_id,
            leakage_risk=self.leakage_risk,
            health_tests=list(self.health_tests),
            failure_action=self.default_failure_action,
        )

    def run_validator(self, spec: ValidatorSpec, payload: dict[str, Any]) -> ValidatorResult:
        if self.run is not None:
            try:
                return self.run(spec, payload)
            except Exception as exc:
                return ValidatorResult(
                    validator_id=spec.validator_id,
                    family_id=self.family_id,
                    claim_ids=list(spec.claim_ids),
                    status="error",
                    authority_used="A0",
                    health_status={"error": True},
                    observations={"error": str(exc)},
                )
        return ValidatorResult(
            validator_id=spec.validator_id,
            family_id=self.family_id,
            claim_ids=list(spec.claim_ids),
            status="abstain",
            authority_used="A0",
            health_status={"implemented": False},
            observations={"reason": "validator family has no runner"},
        )


class ValidatorRegistry:
    def __init__(self, families: Iterable[ValidatorFamily] | None = None) -> None:
        self._families: dict[str, ValidatorFamily] = {}
        for family in families or []:
            self.register(family)

    def register(self, family: ValidatorFamily) -> None:
        if family.family_id in self._families:
            raise ValueError(f"duplicate validator family {family.family_id!r}")
        self._families[family.family_id] = family

    def get(self, family_id: str) -> ValidatorFamily:
        if family_id not in self._families:
            raise KeyError(f"unknown validator family {family_id!r}")
        return self._families[family_id]

    def families(self) -> list[ValidatorFamily]:
        return list(self._families.values())

    def select(self, context: dict[str, Any], *, minimum_score: float = 0.1, limit: int = 6) -> list[ValidatorFamily]:
        scored = [(family.score_applicability(context), family) for family in self._families.values()]
        scored.sort(key=lambda item: (item[0], item[1].family_id), reverse=True)
        return [family for score, family in scored if score >= minimum_score][:limit]

    def make_validator_id(self, family_id: str, claim_ids: list[str], inputs: dict[str, Any] | None = None) -> str:
        return f"validator.{family_id}.{stable_hash(claim_ids, inputs or {})[:12]}"


def default_validator_registry() -> ValidatorRegistry:
    from .families import all_default_families

    return ValidatorRegistry(all_default_families())


__all__ = ["ValidatorFamily", "ValidatorRegistry", "default_validator_registry"]
