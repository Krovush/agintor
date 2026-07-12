from __future__ import annotations

import hashlib
import subprocess
import sys
from copy import deepcopy
from typing import Any

import pytest
from pydantic import ValidationError

from agintor.contracts.epochs import (
    PublicReproductionStep,
    TaskCeilings,
    TaskEnvelope,
    WorkspaceSnapshotRef,
)
from agintor.contracts.harness import (
    DependencyRef,
    HarnessProtocol,
    RuntimeDependencyManifest,
    TrustedToolDependency,
)
from agintor.runtime.api.composite_compiler import (
    CompositeCompilationError,
    InertProtocolError,
    assert_semantic_change,
    compile_composite_run_plan,
    load_canonical_harness_seed,
    load_composite_compiler_metadata,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _task() -> TaskEnvelope:
    return TaskEnvelope(
        task_manifest_id="public-repair-task",
        epoch_id="repo-repair-development",
        epoch_manifest_digest=_digest("epoch"),
        data_state="development",
        split_manifest_digest=_digest("development-split"),
        issue="Repair the observed public failure without target-file hints.",
        workspace_snapshot=WorkspaceSnapshotRef(
            snapshot_id="public-snapshot",
            uri="host-private://must-not-enter-actor-context",
            digest=_digest("snapshot"),
        ),
        public_reproduction=(
            PublicReproductionStep(
                step_id="public-test",
                argv=("python", "-m", "pytest", "-q"),
                timeout_ms=10_000,
            ),
        ),
        ceilings=TaskCeilings(
            max_model_calls=6,
            max_input_tokens=20_000,
            max_output_tokens=8_000,
            max_cached_tokens=0,
            max_tool_calls=30,
            max_tool_output_bytes=200_000,
            max_artifact_bytes=40_000,
            max_patch_bytes=20_000,
            max_retries=1,
            max_wall_time_ms=120_000,
            provider_deadline_ms=60_000,
            max_known_cost_usd=1.0,
            max_estimated_cost_usd=2.0,
        ),
    )


def _dependencies(task: TaskEnvelope) -> RuntimeDependencyManifest:
    tools = tuple(
        TrustedToolDependency(
            tool_id=tool_id,
            interface_version="1",
            implementation_digest=_digest(f"implementation:{tool_id}"),
            policy_digest=_digest(f"policy:{tool_id}"),
        )
        for tool_id in sorted(task.allowed_capabilities)
    )
    return RuntimeDependencyManifest(
        compiler=DependencyRef(
            dependency_id="agintor.composite_compiler",
            interface_version="1",
            implementation_digest=_digest("composite-compiler"),
        ),
        harness_contract=DependencyRef(
            dependency_id="agintor.harness_protocol",
            interface_version="repo-repair-harness-v1",
            implementation_digest=_digest("harness-contract"),
        ),
        kernel=DependencyRef(
            dependency_id="agintor.runtime_kernel",
            interface_version="1",
            implementation_digest=_digest("runtime-kernel"),
        ),
        trusted_tools=tools,
    )


def _seed_payload() -> dict[str, Any]:
    return load_canonical_harness_seed().protocol.model_dump(mode="python")


def _compiled(protocol: HarnessProtocol | dict[str, Any]):
    task = _task()
    return compile_composite_run_plan(task, protocol, _dependencies(task))


def _three_actor_protocol() -> HarnessProtocol:
    return HarnessProtocol.model_validate(
        {
            "actors": [
                {
                    "actor_id": "investigator-a",
                    "task_view": ["issue", "public_reproduction", "workspace"],
                    "instruction": "Localize the failure from source and public checks.",
                    "tool_ids": ["repo.search", "repo.read", "repo.public_test"],
                    "budget_share_bps": 2500,
                },
                {
                    "actor_id": "investigator-b",
                    "task_view": ["issue", "workspace"],
                    "instruction": "Independently inspect likely cross-file interactions.",
                    "tool_ids": ["repo.search", "repo.read", "repo.diff"],
                    "budget_share_bps": 2500,
                },
                {
                    "actor_id": "implementer",
                    "task_view": ["issue", "workspace"],
                    "instruction": "Integrate both findings, repair the repository, and emit a diff.",
                    "tool_ids": [
                        "repo.search",
                        "repo.read",
                        "repo.edit",
                        "repo.diff",
                        "repo.public_test",
                    ],
                    "budget_share_bps": 5000,
                },
            ],
            "artifact_channels": [
                {
                    "channel_id": "finding-a",
                    "producer_actor_id": "investigator-a",
                    "consumer_actor_id": "implementer",
                },
                {
                    "channel_id": "finding-b",
                    "producer_actor_id": "investigator-b",
                    "consumer_actor_id": "implementer",
                },
            ],
            "termination": {"final_actor_id": "implementer"},
        }
    )


def _revision_protocol() -> HarnessProtocol:
    return HarnessProtocol.model_validate(
        {
            "actors": [
                {
                    "actor_id": "implementer",
                    "task_view": ["issue", "public_reproduction", "workspace"],
                    "instruction": "Produce a tested draft repair and then a final diff if revised.",
                    "tool_ids": [
                        "repo.search",
                        "repo.read",
                        "repo.edit",
                        "repo.diff",
                        "repo.public_test",
                    ],
                    "budget_share_bps": 6500,
                },
                {
                    "actor_id": "reviewer",
                    "task_view": ["issue", "public_reproduction", "workspace"],
                    "instruction": "Review the delivered draft against source and public checks.",
                    "tool_ids": ["repo.read", "repo.diff", "repo.public_test"],
                    "budget_share_bps": 3500,
                },
            ],
            "artifact_channels": [
                {
                    "channel_id": "draft",
                    "producer_actor_id": "implementer",
                    "consumer_actor_id": "reviewer",
                },
                {
                    "channel_id": "feedback",
                    "producer_actor_id": "reviewer",
                    "consumer_actor_id": "implementer",
                },
            ],
            "revision": {
                "actor_id": "implementer",
                "feedback_channel_id": "feedback",
                "instruction": "Use the immutable review feedback to revise the draft once.",
            },
            "termination": {"final_actor_id": "implementer"},
        }
    )


def test_tracked_seed_and_compiler_metadata_load_and_bind_source_digest() -> None:
    metadata = load_composite_compiler_metadata()
    seed = load_canonical_harness_seed()

    assert metadata.canonical_seed == seed.reference
    assert seed.protocol.source_digest() == seed.reference.source_protocol_digest
    assert len(seed.protocol.actors) == 2
    assert len(seed.protocol.artifact_channels) == 1


def test_compiler_contracts_and_seed_load_from_source_hidden_sdk_bundle(
    tmp_path,
) -> None:
    from agintor.runtime.sdk.bundle import bundle_runtime_kernel

    runtime_dir = tmp_path / "runtime"
    manifest = bundle_runtime_kernel(runtime_dir)
    required_paths = {
        "agintor_runtime/contracts/epochs.py",
        "agintor_runtime/contracts/harness.py",
        "agintor_runtime/runtime/api/composite_compiler.py",
        "agintor_runtime/templates/harness/composite_compiler_metadata.json",
        "agintor_runtime/templates/harness/repo_repair_v1_two_actor_seed.json",
    }
    assert required_paths <= set(manifest.files)

    bundle_root = runtime_dir / "runtime_sdk"
    script = (
        "import sys; "
        f"sys.path.insert(0, {str(bundle_root)!r}); "
        "from agintor_runtime.runtime.api.composite_compiler import "
        "load_canonical_harness_seed; "
        "seed = load_canonical_harness_seed(); "
        "print(seed.reference.source_protocol_digest)"
    )
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == load_canonical_harness_seed().protocol.source_digest()


def test_same_public_task_two_protocols_compile_to_distinct_semantics() -> None:
    first_payload = _seed_payload()
    second_payload = deepcopy(first_payload)
    second_payload["actors"][0]["instruction"] = (
        "Inspect public behavior and produce a competing evidence-first diagnosis."
    )

    first = _compiled(first_payload)
    second = _compiled(second_payload)

    assert first.task_envelope_digest == second.task_envelope_digest
    assert first.source_protocol_digest != second.source_protocol_digest
    assert first.actor_calls != second.actor_calls
    assert first.compiled_semantic_digest != second.compiled_semantic_digest
    assert_semantic_change(first, second)


def test_compiled_seed_has_exact_artifact_delivery_and_no_host_or_sealed_data() -> None:
    plan = _compiled(_seed_payload())
    investigator, implementer = plan.actor_calls

    assert investigator.call_id == "actor.investigator.initial"
    assert implementer.call_id == "actor.implementer.initial"
    assert plan.artifact_deliveries[0].producer_call_id == investigator.call_id
    assert plan.artifact_deliveries[0].consumer_call_id == implementer.call_id
    assert any(
        read.source_ref == "artifact.investigation"
        for read in implementer.context_reads
    )
    assert any(
        read.source_kind == "workspace" and read.source_ref == "scratch_workspace"
        for read in investigator.context_reads
    )

    payload = plan.model_dump(mode="json")
    serialized = str(payload)
    assert "host-private://must-not-enter-actor-context" not in serialized
    assert _task().issue not in serialized

    forbidden_keys = {
        "operations",
        "target_files",
        "gold_patch",
        "hidden_checks",
        "sealed_fixture",
        "private_expected",
    }

    def keys(value: Any) -> set[str]:
        if isinstance(value, dict):
            return set(value) | {
                child
                for item in value.values()
                for child in keys(item)
            }
        if isinstance(value, list):
            return {child for item in value for child in keys(item)}
        return set()

    assert not (keys(payload) & forbidden_keys)


def test_liveness_manifest_covers_exact_mutable_surface_with_runtime_owners() -> None:
    protocol = load_canonical_harness_seed().protocol
    plan = _compiled(protocol)
    bindings = plan.liveness_manifest.bindings

    assert {binding.source_path for binding in bindings} == set(
        protocol.mutable_field_paths()
    )
    assert all(binding.plan_consumer_paths for binding in bindings)
    assert all(binding.runtime_owners for binding in bindings)
    assert plan.liveness_manifest.consumer_paths_for(
        "actors[investigator].instruction"
    ) == ("actor_calls[actor.investigator.initial].instruction",)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload["actors"][0].__setitem__(
            "instruction", "Use a symbol-first investigation strategy."
        ),
        lambda payload: payload["actors"][0].__setitem__(
            "task_view", ["issue", "workspace", "public_reproduction"]
        ),
        lambda payload: payload["actors"][0].__setitem__(
            "tool_ids", ["repo.search", "repo.read", "repo.public_test"]
        ),
        lambda payload: (
            payload["actors"][0].__setitem__("budget_share_bps", 3500),
            payload["actors"][1].__setitem__("budget_share_bps", 6500),
        ),
    ],
)
def test_each_scalar_mutation_family_changes_compiled_consumer_and_digest(
    mutate,
) -> None:
    parent_payload = _seed_payload()
    candidate_payload = deepcopy(parent_payload)
    mutate(candidate_payload)

    parent = _compiled(parent_payload)
    child = _compiled(candidate_payload)

    assert parent.actor_calls != child.actor_calls
    assert parent.compiled_semantic_digest != child.compiled_semantic_digest


def test_exact_compiled_noop_is_rejected_before_evaluation() -> None:
    payload = _seed_payload()
    parent = _compiled(payload)

    reordered = deepcopy(payload)
    for actor in reordered["actors"]:
        actor["tool_ids"] = list(reversed(actor["tool_ids"]))
    task = _task()

    with pytest.raises(InertProtocolError, match="exact semantic no-op"):
        compile_composite_run_plan(
            task,
            reordered,
            _dependencies(task),
            reject_semantic_digest=parent.compiled_semantic_digest,
        )
    with pytest.raises(InertProtocolError, match="exact semantic no-op"):
        assert_semantic_change(parent, _compiled(reordered))


def test_unsupported_or_unconsumed_source_fields_fail_closed() -> None:
    payload = _seed_payload()
    payload["metadata"] = {"ignored": True}

    with pytest.raises(CompositeCompilationError, match="invalid HarnessProtocol"):
        _compiled(payload)
    with pytest.raises(ValidationError, match="extra_forbidden"):
        HarnessProtocol.model_validate(payload)


def test_three_actor_protocol_lowers_to_deterministic_fork_join() -> None:
    plan = _compiled(_three_actor_protocol())

    assert plan.stages[0].fork is True
    assert plan.stages[0].call_ids == (
        "actor.investigator-a.initial",
        "actor.investigator-b.initial",
    )
    assert plan.stages[1].join is True
    assert plan.stages[1].inbound_channel_ids == ("finding-a", "finding-b")
    assert plan.stages[1].depends_on_stage_ids == ("stage.0",)


def test_revision_protocol_has_one_feedback_delivery_and_one_revision_call() -> None:
    plan = _compiled(_revision_protocol())
    revision_calls = [
        call for call in plan.actor_calls if call.call_kind == "revision"
    ]

    assert len(revision_calls) == 1
    assert plan.budget_ledger.scheduled_revision_calls == 1
    assert plan.stages[-1].revision is True
    assert plan.termination.final_actor_call_id == "actor.implementer.revision"
    feedback = next(
        delivery
        for delivery in plan.artifact_deliveries
        if delivery.channel_id == "feedback"
    )
    assert feedback.consumer_call_id == "actor.implementer.revision"
    assert any(
        read.source_kind == "prior_actor_output"
        for read in revision_calls[0].context_reads
    )


def test_cycles_and_missing_dependency_tools_are_rejected() -> None:
    cycle = _seed_payload()
    cycle["artifact_channels"] = (
        *cycle["artifact_channels"],
        {
            "channel_id": "cycle",
            "producer_actor_id": "implementer",
            "consumer_actor_id": "investigator",
        },
    )
    with pytest.raises(CompositeCompilationError, match="acyclic"):
        _compiled(cycle)

    task = _task()
    dependencies = _dependencies(task)
    incomplete = dependencies.model_dump(mode="python")
    incomplete["trusted_tools"] = incomplete["trusted_tools"][:-1]
    with pytest.raises(
        CompositeCompilationError,
        match="pin every fixed trusted tool",
    ):
        compile_composite_run_plan(
            task,
            load_canonical_harness_seed().protocol,
            incomplete,
        )
