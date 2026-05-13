from __future__ import annotations

from agintor.oracle.validator_registry import default_validator_registry


def test_default_registry_contains_required_families():
    registry = default_validator_registry()
    names = {family.family_id for family in registry.families()}
    assert {"schema_artifact", "repo_patch", "stateful_service", "trace_state", "trading_outcome", "consent_proof"}.issubset(names)
