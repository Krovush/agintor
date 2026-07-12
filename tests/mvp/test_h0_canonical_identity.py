from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import BaseModel

from agintor.core.identity import (
    canonical_identity_bytes,
    canonical_identity_digest,
    composite_plan_digest,
    environment_digest,
    evidence_digest,
    protocol_digest,
    provider_config_digest,
    task_digest,
    transaction_digest,
)


def test_mapping_and_nested_set_order_do_not_change_identity() -> None:
    first = {
        "outer": {"z": [3, 2, 1], "members": {"beta", "alpha"}},
        "path": Path("workspace") / "repo",
    }
    second = {
        "path": Path("workspace/repo"),
        "outer": {"members": {"alpha", "beta"}, "z": [3, 2, 1]},
    }

    assert canonical_identity_bytes(first) == canonical_identity_bytes(second)
    assert task_digest(first) == task_digest(second)


def test_type_tags_prevent_equivalent_looking_value_collisions() -> None:
    values = [
        None,
        False,
        0,
        0.0,
        "0",
        b"0",
        Path("0"),
        [],
        (),
        set(),
        frozenset(),
        {},
    ]

    encodings = [canonical_identity_bytes(value) for value in values]

    assert len(encodings) == len(set(encodings))
    assert canonical_identity_bytes({"value": b"same"}) != canonical_identity_bytes({"value": "same"})
    assert canonical_identity_bytes({"value": 1}) != canonical_identity_bytes({"value": "1"})


def test_identity_domains_cannot_collide_for_the_same_payload() -> None:
    payload = {"id": "shared", "revision": 1}
    digests = {
        task_digest(payload),
        protocol_digest(payload),
        composite_plan_digest(payload),
        environment_digest(payload),
        provider_config_digest(payload),
        transaction_digest(payload),
        evidence_digest(payload),
    }

    assert len(digests) == 7
    assert canonical_identity_digest(payload, domain="task") == task_digest(payload)


def test_validated_models_share_identity_with_their_semantic_payload() -> None:
    class ExampleContract(BaseModel):
        name: str
        members: set[int]

    model = ExampleContract(name="example", members={3, 1, 2})

    assert protocol_digest(model) == protocol_digest(model.model_dump(mode="python"))


def test_cross_process_digest_is_stable_across_hash_seeds_and_order() -> None:
    script = """
import json
from pathlib import Path
from agintor.core.identity import canonical_identity_digest

payload = {
    "nested": {"members": set(%s), "bytes": b"payload"},
    "path": Path("repo") / "src" / "module.py",
    "typed": [None, False, 0, 0.0, "0"],
}
print(json.dumps({"digest": canonical_identity_digest(payload, domain="cross-process")}))
"""
    commands = [script % repr(order) for order in (["a", "b", "c"], ["c", "a", "b"])]
    outputs = []
    for hash_seed, command in zip(("1", "987654"), commands):
        completed = subprocess.run(
            [sys.executable, "-c", command],
            check=True,
            capture_output=True,
            text=True,
            env={**__import__("os").environ, "PYTHONHASHSEED": hash_seed},
        )
        outputs.append(json.loads(completed.stdout)["digest"])

    assert outputs[0] == outputs[1]


def test_unsupported_and_cyclic_values_are_rejected() -> None:
    class Unsupported:
        pass

    cyclic: list[object] = []
    cyclic.append(cyclic)

    with pytest.raises(TypeError, match="does not support"):
        canonical_identity_bytes(Unsupported())
    with pytest.raises(ValueError, match="cyclic"):
        canonical_identity_bytes(cyclic)
    with pytest.raises(ValueError, match="domain"):
        canonical_identity_digest({}, domain="  ")
