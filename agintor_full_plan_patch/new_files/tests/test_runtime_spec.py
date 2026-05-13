from __future__ import annotations

import pytest

from agintor.contracts import default_langgraph_runtime_spec, runtime_spec_digest


def test_runtime_spec_digest_stable():
    spec = default_langgraph_runtime_spec(runtime_id="r1", name="Runtime")
    assert runtime_spec_digest(spec) == runtime_spec_digest(spec.model_dump(mode="json"))


def test_runtime_spec_rejects_private_fields():
    payload = default_langgraph_runtime_spec(runtime_id="r1", name="Runtime").model_dump(mode="json")
    payload["metadata"] = {"private_expected": "do not leak"}
    with pytest.raises(ValueError):
        type(default_langgraph_runtime_spec(runtime_id="r2", name="Runtime")).model_validate(payload)
