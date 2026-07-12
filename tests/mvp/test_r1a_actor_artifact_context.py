from __future__ import annotations

import hashlib
import json
import threading
import time
from copy import deepcopy
from typing import Any

import pytest

from agintor.contracts.epochs import (
    PublicReproductionStep,
    TaskCeilings,
    TaskEnvelope,
    WorkspaceSnapshotRef,
)
from agintor.contracts.harness import (
    CompositeRunPlan,
    ContextReadPlan,
    DependencyRef,
    HarnessPublicCarryoverRef,
    HarnessPublicSessionContext,
    HarnessPublicSessionLimits,
    HarnessProtocol,
    RuntimeDependencyManifest,
    TrustedToolDependency,
)
from agintor.core.identity import composite_plan_digest
from agintor.runtime.api.composite_compiler import (
    compile_composite_run_plan,
    load_canonical_harness_seed,
)
from agintor.runtime.kernel.composite_artifacts import (
    ArtifactStoreError,
    ImmutableArtifactStore,
)
from agintor.runtime.kernel.composite_budget import (
    CostStatus,
    ProviderUsageReport,
    UsageStatus,
)
from agintor.runtime.kernel.composite_provider import (
    CredentialReference,
    ProviderCallControl,
    ProviderInvocation,
)
from agintor.runtime.kernel.composite_runtime import (
    ActorCallOutput,
    ActorCallRequest,
    CompositeRuntime,
    CompositeRuntimeError,
    ScratchWorkspaceBinding,
)
from agintor.utils import count_tokens_rough


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _task() -> TaskEnvelope:
    return TaskEnvelope(
        task_manifest_id="public-repair-task",
        epoch_id="repo-repair-development",
        epoch_manifest_digest=_digest("epoch"),
        data_state="development",
        split_manifest_digest=_digest("development-split"),
        issue="Repair the public failure without hidden target-file hints.",
        workspace_snapshot=WorkspaceSnapshotRef(
            snapshot_id="clean-snapshot",
            uri="host-private://source-uri-must-never-enter-context",
            digest=_digest("clean-snapshot"),
        ),
        public_reproduction=(
            PublicReproductionStep(
                step_id="public-test",
                argv=("python", "-m", "pytest", "-q"),
                timeout_ms=10_000,
            ),
        ),
        ceilings=TaskCeilings(
            max_model_calls=8,
            max_input_tokens=30_000,
            max_output_tokens=12_000,
            max_cached_tokens=0,
            max_tool_calls=30,
            max_tool_output_bytes=200_000,
            max_artifact_bytes=40_000,
            max_patch_bytes=20_000,
            max_retries=1,
            max_wall_time_ms=30_000,
            provider_deadline_ms=5_000,
            max_known_cost_usd=1.0,
            max_estimated_cost_usd=2.0,
        ),
    )


def _dependencies(task: TaskEnvelope) -> RuntimeDependencyManifest:
    return RuntimeDependencyManifest(
        compiler=DependencyRef(
            dependency_id="agintor.composite_compiler",
            interface_version="1",
            implementation_digest=_digest("compiler"),
        ),
        harness_contract=DependencyRef(
            dependency_id="agintor.harness_protocol",
            interface_version="repo-repair-harness-v1",
            implementation_digest=_digest("harness"),
        ),
        kernel=DependencyRef(
            dependency_id="agintor.runtime_kernel",
            interface_version="1",
            implementation_digest=_digest("kernel"),
        ),
        trusted_tools=tuple(
            TrustedToolDependency(
                tool_id=tool_id,
                interface_version="1",
                implementation_digest=_digest(f"implementation:{tool_id}"),
                policy_digest=_digest(f"policy:{tool_id}"),
            )
            for tool_id in sorted(task.allowed_capabilities)
        ),
    )


def _compile(protocol: HarnessProtocol | dict[str, Any]):
    task = _task()
    return task, compile_composite_run_plan(task, protocol, _dependencies(task))


def _patch(label: str = "fixed") -> str:
    return (
        "--- a/pkg/example.py\n"
        "+++ b/pkg/example.py\n"
        "@@ -1 +1 @@\n"
        "-old\n"
        f"+{label}\n"
    )


def _public_session_context(
    *,
    session_id: str = "session.alpha",
    summary: str = "Public carryover summary from the last same-release solve.",
) -> HarnessPublicSessionContext:
    return HarnessPublicSessionContext(
        session_id=session_id,
        active_release_digest=_digest("release"),
        session_manifest_digest=_digest(f"manifest:{session_id}"),
        parent_message_id="hmsg.parent",
        next_sequence=2,
        limits=HarnessPublicSessionLimits(
            max_entries=2,
            max_total_bytes=1024,
            max_summary_bytes=128,
        ),
        carryover=(
            HarnessPublicCarryoverRef(
                artifact_ref="artifacts/public-summary.json",
                artifact_digest=_digest("public-summary"),
                summary=summary,
            ),
        ),
    )


class ScriptedActorProvider:
    def __init__(self, outputs: dict[str, ActorCallOutput], *, pause_s: float = 0.0) -> None:
        self.outputs = outputs
        self.pause_s = pause_s
        self.requests: list[ActorCallRequest] = []
        self.credentials: list[CredentialReference | None] = []
        self._lock = threading.Lock()
        self._active = 0
        self.max_active = 0

    def invoke(
        self,
        request: Any,
        *,
        control: ProviderCallControl,
        credential_reference: CredentialReference | None,
    ) -> ProviderInvocation:
        normalized = ActorCallRequest.model_validate(request)
        with self._lock:
            self._active += 1
            self.max_active = max(self.max_active, self._active)
        try:
            if self.pause_s:
                time.sleep(self.pause_s)
            self.requests.append(normalized)
            self.credentials.append(credential_reference)
            output = self.outputs[normalized.call_id]
            output_tokens = count_tokens_rough(output.model_dump_json())
            return ProviderInvocation(
                response=output,
                usage=ProviderUsageReport(
                    usage_status=UsageStatus.KNOWN,
                    input_tokens=normalized.input_token_estimate,
                    output_tokens=output_tokens,
                    cached_tokens=0,
                    cost_status=CostStatus.KNOWN,
                    cost_usd=0.0,
                    response_id=f"scripted.{normalized.call_id}",
                ),
            )
        finally:
            with self._lock:
                self._active -= 1


def _runtime(task, plan, provider, **kwargs) -> CompositeRuntime:
    return CompositeRuntime(
        plan,
        task,
        ScratchWorkspaceBinding(
            workspace_id="scratch.run-001",
            workspace_digest=task.workspace_snapshot.digest,
        ),
        provider,
        run_id="run.r1a.offline",
        **kwargs,
    )


def test_two_actor_runtime_delivers_exact_artifact_in_differentiated_context() -> None:
    task, plan = _compile(load_canonical_harness_seed().protocol)
    finding = "Failure originates in parser normalization; preserve the public API."
    provider = ScriptedActorProvider(
        {
            "actor.investigator.initial": ActorCallOutput(
                output_text="Investigation complete.",
                artifact_payloads={"artifact.investigation": finding},
            ),
            "actor.implementer.initial": ActorCallOutput(
                output_text="Implemented the repair.",
                final_patch=_patch(),
            ),
        }
    )

    result = _runtime(task, plan, provider).run()

    assert [request.call_id for request in provider.requests] == [
        "actor.investigator.initial",
        "actor.implementer.initial",
    ]
    investigator_reads = {
        read.source_ref: read for read in provider.requests[0].context.reads
    }
    implementer_reads = {
        read.source_ref: read for read in provider.requests[1].context.reads
    }
    assert set(investigator_reads) == {
        "issue",
        "public_reproduction",
        "public_carryover",
        "scratch_workspace",
    }
    assert set(implementer_reads) == {
        "issue",
        "public_carryover",
        "scratch_workspace",
        "artifact.investigation",
    }
    assert "public_reproduction" not in implementer_reads
    assert investigator_reads["public_carryover"].source_kind == "session"
    assert implementer_reads["public_carryover"].source_kind == "session"
    assert investigator_reads["public_carryover"].value["carryover"] == []
    assert implementer_reads["public_carryover"].value["carryover"] == []
    assert implementer_reads["artifact.investigation"].value == finding
    artifact = result.artifacts[0].artifact
    assert implementer_reads["artifact.investigation"].value_digest == artifact.payload_digest
    assert result.artifact_deliveries[0].payload == finding
    assert result.artifact_deliveries[0].payload_digest == artifact.payload_digest
    assert result.artifacts[0].actual_consumer_call_ids == (
        "actor.implementer.initial",
    )
    assert all(manifest.assembled_before_provider for manifest in result.context_manifests)
    assert result.actor_calls[1].actual_read_ids == tuple(
        read.read_id for read in provider.requests[1].context.reads
    )
    assert result.final_patch == _patch()
    assert result.public_verification_status == "not_run"
    assert result.budget.model_calls == 2
    assert result.budget.reconciled is True
    assert result.budget.healthy is True

    serialized_requests = json.dumps(
        [request.model_dump(mode="json") for request in provider.requests],
        sort_keys=True,
    )
    assert task.workspace_snapshot.uri not in serialized_requests
    assert task.workspace_snapshot.uri not in result.model_dump_json()
    workspace_value = investigator_reads["scratch_workspace"].value
    assert workspace_value == {
        "workspace_id": "scratch.run-001",
        "workspace_digest": task.workspace_snapshot.digest,
    }


def test_declared_public_session_context_is_actor_visible_metadata_only() -> None:
    task, plan = _compile(load_canonical_harness_seed().protocol)
    public_session = _public_session_context()
    provider = ScriptedActorProvider(
        {
            "actor.investigator.initial": ActorCallOutput(
                output_text="Investigation complete.",
                artifact_payloads={"artifact.investigation": "public diagnosis"},
            ),
            "actor.implementer.initial": ActorCallOutput(
                output_text="Implemented the repair.",
                final_patch=_patch(),
            ),
        }
    )

    result = _runtime(
        task,
        plan,
        provider,
        public_session_context=public_session,
    ).run()

    session_reads = [
        next(read for read in request.context.reads if read.source_kind == "session")
        for request in provider.requests
    ]
    assert [read.provenance_ref for read in session_reads] == [
        public_session.context_digest,
        public_session.context_digest,
    ]
    assert session_reads[0].value == public_session.actor_visible_value()
    assert session_reads[0].value["carryover"] == [
        public_session.carryover[0].model_dump(mode="json")
    ]
    serialized = json.dumps(
        [read.value for read in session_reads],
        sort_keys=True,
    )
    assert "Public carryover summary" in serialized
    assert "payload" not in serialized.casefold()
    assert "repository_snapshot" not in serialized.casefold()
    assert "workspace_snapshot" not in serialized.casefold()
    recorded_session_read = next(
        read for read in result.context_manifests[0].reads if read.source_kind == "session"
    )
    assert recorded_session_read.value_digest == session_reads[0].value_digest


def test_public_session_context_changes_request_identity_without_crossing_sessions() -> None:
    task, plan = _compile(load_canonical_harness_seed().protocol)
    first_provider = ScriptedActorProvider(
        {
            "actor.investigator.initial": ActorCallOutput(
                output_text="Investigation complete.",
                artifact_payloads={"artifact.investigation": "first"},
            ),
            "actor.implementer.initial": ActorCallOutput(
                output_text="Implemented the repair.",
                final_patch=_patch("first"),
            ),
        }
    )
    second_provider = ScriptedActorProvider(
        {
            "actor.investigator.initial": ActorCallOutput(
                output_text="Investigation complete.",
                artifact_payloads={"artifact.investigation": "second"},
            ),
            "actor.implementer.initial": ActorCallOutput(
                output_text="Implemented the repair.",
                final_patch=_patch("second"),
            ),
        }
    )

    first_session = _public_session_context(
        session_id="session.alpha",
        summary="Public alpha carryover.",
    )
    second_session = _public_session_context(
        session_id="session.beta",
        summary="Public beta carryover.",
    )

    _runtime(task, plan, first_provider, public_session_context=first_session).run()
    _runtime(task, plan, second_provider, public_session_context=second_session).run()

    first_read = next(
        read for read in first_provider.requests[0].context.reads if read.source_kind == "session"
    )
    second_read = next(
        read for read in second_provider.requests[0].context.reads if read.source_kind == "session"
    )
    assert first_read.value["session_id"] == "session.alpha"
    assert second_read.value["session_id"] == "session.beta"
    assert first_read.value_digest != second_read.value_digest
    assert first_provider.requests[0].request_digest != second_provider.requests[0].request_digest


def test_supplied_session_context_requires_declared_actor_read() -> None:
    payload = {
        "actors": [
            {
                "actor_id": "solo",
                "task_view": ["issue", "workspace"],
                "instruction": "Repair the public issue.",
                "tool_ids": ["repo.read"],
                "budget_share_bps": 10_000,
            }
        ],
        "artifact_channels": [],
        "revision": None,
        "termination": {"final_actor_id": "solo"},
    }
    task, plan = _compile(payload)
    provider = ScriptedActorProvider(
        {
            "actor.solo.initial": ActorCallOutput(
                output_text="Implemented the repair.",
                final_patch=_patch(),
            ),
        }
    )

    with pytest.raises(CompositeRuntimeError, match="public session context was supplied"):
        _runtime(
            task,
            plan,
            provider,
            public_session_context=_public_session_context(),
        )


def test_public_session_context_enforces_runtime_size_bounds() -> None:
    with pytest.raises(ValueError, match="summary byte limit"):
        HarnessPublicSessionContext(
            session_id="session.too-large",
            active_release_digest=_digest("release"),
            session_manifest_digest=_digest("manifest"),
            parent_message_id="hmsg.parent",
            next_sequence=1,
            limits=HarnessPublicSessionLimits(
                max_entries=2,
                max_total_bytes=1024,
                max_summary_bytes=8,
            ),
            carryover=(
                HarnessPublicCarryoverRef(
                    artifact_ref="artifacts/public.json",
                    artifact_digest=_digest("public"),
                    summary="definitely too long",
                ),
            ),
        )


def test_artifact_store_is_bounded_directed_and_write_once() -> None:
    task, plan = _compile(load_canonical_harness_seed().protocol)
    store = ImmutableArtifactStore(plan, max_total_bytes=8)
    write = plan.actor_calls[0].artifact_writes[0]
    artifact = store.write(
        write,
        producer_call_id=write.producer_call_id,
        payload="finding",
    )

    assert artifact.byte_size == 7
    with pytest.raises(ArtifactStoreError, match="immutable"):
        store.write(
            write,
            producer_call_id=write.producer_call_id,
            payload="changed",
        )
    with pytest.raises(ArtifactStoreError, match="not declared"):
        store.deliver(
            artifact_id=write.artifact_id,
            consumer_call_id="actor.undeclared.initial",
        )

    second_store = ImmutableArtifactStore(plan, max_total_bytes=3)
    with pytest.raises(ArtifactStoreError, match="aggregate artifact"):
        second_store.write(
            write,
            producer_call_id=write.producer_call_id,
            payload="large",
        )


def test_missing_or_undeclared_artifacts_fail_before_downstream_provider_call() -> None:
    task, plan = _compile(load_canonical_harness_seed().protocol)
    missing_output = ScriptedActorProvider(
        {
            "actor.investigator.initial": ActorCallOutput(
                output_text="No declared artifact returned.",
            ),
        }
    )
    with pytest.raises(CompositeRuntimeError) as missing:
        _runtime(task, plan, missing_output).run()
    assert missing.value.kind == "artifact_write_mismatch"
    assert [request.call_id for request in missing_output.requests] == [
        "actor.investigator.initial"
    ]

    implementer = plan.actor_calls[1]
    forged_read = ContextReadPlan(
        read_id="artifact.forged",
        source_kind="artifact",
        source_ref="artifact.forged",
    )
    forged_call = implementer.model_copy(
        update={"context_reads": (*implementer.context_reads, forged_read)}
    )
    unbound_forged_plan = plan.model_copy(
        update={"actor_calls": (plan.actor_calls[0], forged_call)}
    )
    forged_payload = unbound_forged_plan.model_dump(mode="python")
    forged_payload["compiled_semantic_digest"] = composite_plan_digest(
        unbound_forged_plan.semantic_payload()
    )
    forged_plan = CompositeRunPlan.model_validate(forged_payload)
    with pytest.raises(CompositeRuntimeError) as undeclared:
        _runtime(task, forged_plan, missing_output)
    assert undeclared.value.kind == "undelivered_artifact_read"


def _three_actor_protocol() -> HarnessProtocol:
    return HarnessProtocol.model_validate(
        {
            "actors": [
                {
                    "actor_id": "investigator-a",
                    "task_view": ["issue", "workspace"],
                    "instruction": "Inspect component A and report evidence.",
                    "tool_ids": ["repo.search", "repo.read"],
                    "budget_share_bps": 2500,
                },
                {
                    "actor_id": "investigator-b",
                    "task_view": ["issue", "public_reproduction", "workspace"],
                    "instruction": "Inspect component B and reproduce the failure.",
                    "tool_ids": ["repo.read", "repo.public_test"],
                    "budget_share_bps": 2500,
                },
                {
                    "actor_id": "implementer",
                    "task_view": ["issue", "workspace"],
                    "instruction": "Combine both delivered findings and emit a patch.",
                    "tool_ids": ["repo.read", "repo.edit", "repo.diff"],
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


def test_logical_fork_join_executes_in_deterministic_tuple_order_without_concurrency() -> None:
    task, plan = _compile(_three_actor_protocol())
    provider = ScriptedActorProvider(
        {
            "actor.investigator-a.initial": ActorCallOutput(
                output_text="A complete",
                artifact_payloads={"artifact.finding-a": "finding A"},
            ),
            "actor.investigator-b.initial": ActorCallOutput(
                output_text="B complete",
                artifact_payloads={"artifact.finding-b": "finding B"},
            ),
            "actor.implementer.initial": ActorCallOutput(
                output_text="Integrated",
                final_patch=_patch("joined"),
            ),
        },
        pause_s=0.005,
    )

    result = _runtime(task, plan, provider).run()

    expected_order = [call_id for stage in plan.stages for call_id in stage.call_ids]
    assert [request.call_id for request in provider.requests] == expected_order
    assert provider.max_active == 1
    assert result.stages[0].logical_fork is True
    assert result.stages[0].call_execution_order == plan.stages[0].call_ids
    assert result.stages[1].logical_join is True
    final_artifact_reads = [
        read.value
        for read in provider.requests[-1].context.reads
        if read.source_kind == "artifact"
    ]
    assert final_artifact_reads == ["finding A", "finding B"]


def _revision_protocol() -> HarnessProtocol:
    return HarnessProtocol.model_validate(
        {
            "actors": [
                {
                    "actor_id": "implementer",
                    "task_view": ["issue", "workspace"],
                    "instruction": "Prepare a draft repair for review.",
                    "tool_ids": ["repo.read", "repo.edit", "repo.diff"],
                    "budget_share_bps": 6500,
                },
                {
                    "actor_id": "reviewer",
                    "task_view": ["issue", "public_reproduction", "workspace"],
                    "instruction": "Review the delivered draft.",
                    "tool_ids": ["repo.read", "repo.public_test", "repo.diff"],
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
                "instruction": "Revise once using the immutable review.",
            },
            "termination": {"final_actor_id": "implementer"},
        }
    )


def test_revision_reads_prior_output_and_retained_delivered_artifact_once() -> None:
    task, plan = _compile(_revision_protocol())
    provider = ScriptedActorProvider(
        {
            "actor.implementer.initial": ActorCallOutput(
                output_text="draft output text",
                artifact_payloads={"artifact.draft": "draft artifact"},
            ),
            "actor.reviewer.initial": ActorCallOutput(
                output_text="review complete",
                artifact_payloads={"artifact.feedback": "review feedback"},
            ),
            "actor.implementer.revision": ActorCallOutput(
                output_text="revision complete",
                final_patch=_patch("revised"),
            ),
        }
    )

    result = _runtime(task, plan, provider).run()
    revision = provider.requests[-1]
    values = {read.source_ref: read.value for read in revision.context.reads}

    assert values["actor.implementer.initial"] == "draft output text"
    assert values["artifact.feedback"] == "review feedback"
    assert result.final_patch == _patch("revised")
    assert result.stages[-1].call_execution_order == (
        "actor.implementer.revision",
    )


def test_scratch_binding_and_tool_authority_fail_closed() -> None:
    task, plan = _compile(load_canonical_harness_seed().protocol)
    provider = ScriptedActorProvider({})
    with pytest.raises(CompositeRuntimeError) as mismatch:
        CompositeRuntime(
            plan,
            task,
            ScratchWorkspaceBinding(
                workspace_id="scratch.foreign",
                workspace_digest=_digest("foreign"),
            ),
            provider,
            run_id="run.foreign",
        )
    assert mismatch.value.kind == "scratch_identity_mismatch"

    runtime = _runtime(task, plan, provider)
    with pytest.raises(CompositeRuntimeError) as unauthorized:
        runtime.invoke_tool(
            call_id="actor.investigator.initial",
            tool_id="repo.edit",
            arguments={},
            tool_request_id="test.request.unauthorized",
        )
    assert unauthorized.value.kind == "tool_not_authorized"
    with pytest.raises(CompositeRuntimeError) as unavailable:
        runtime.invoke_tool(
            call_id="actor.investigator.initial",
            tool_id="repo.read",
            arguments={"path": "pkg/example.py"},
            tool_request_id="test.request.unavailable",
        )
    assert unavailable.value.kind == "tool_interface_unavailable"
