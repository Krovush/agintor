from __future__ import annotations

import pytest

from agintor.contracts import baseline_langgraph_runtime_spec, RuntimeSpec


def test_runtime_spec_digest_is_stable():
    spec = baseline_langgraph_runtime_spec(runtime_id="r1")
    assert spec.spec_digest == RuntimeSpec.model_validate(spec.model_dump(mode="json")).spec_digest


def test_runtime_spec_rejects_private_oracle_material():
    spec = baseline_langgraph_runtime_spec(runtime_id="r2")
    payload = spec.model_dump(mode="json")
    payload["metadata"] = {"private_expected": "secret"}
    with pytest.raises(ValueError, match="private/sealed oracle"):
        RuntimeSpec.model_validate(payload)
