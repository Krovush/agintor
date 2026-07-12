from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError

from agintor.factory.harness_replay import (
    EvaluatorReplayRow,
    HarnessFactoryReplayCallbacks,
    HarnessFactoryReplayExhaustedError,
    HarnessFactoryReplayIncompleteError,
    HarnessFactoryReplayRecorder,
    HarnessFactoryReplayValidationError,
    ProposalReplayRow,
    ReplayEvaluatorCallback,
    ReplayProposalCallback,
    build_harness_factory_release_from_replay,
    build_harness_factory_replay_manifest,
    harness_evaluation_request_digest,
    harness_evaluation_request_identity,
    load_harness_factory_replay_manifest,
    proposal_batch_request_digest,
    proposal_batch_request_identity,
    write_harness_factory_replay_manifest,
)
from agintor.factory.harness_service import build_harness_factory_release
from agintor.search.paired_harness import PairedSearchIntegrityError
from tests.mvp.test_harness_factory_service import (
    _build_input,
    _gain_proposals,
)
from tests.mvp.test_s1_paired_search import _proof_binding, _receipt


def _multitask_evaluator(build_input, dependencies, calls):
    task_by_id = {task.task_manifest_id: task for task in build_input.task_panel}

    def evaluate(request):
        calls.append(request)
        proof_bindings = []
        for pair_key in request.expected_pair_keys:
            if request.arm_kind in {"search_parent", "control"}:
                complete = pair_key.sampling_replicate == 0
            else:
                complete = any(
                    channel.channel_id.startswith("gain-")
                    for channel in request.protocol.artifact_channels
                ) or pair_key.sampling_replicate == 0
            receipt = _receipt(
                    request=request,
                    pair_key=pair_key,
                    epoch=build_input.epoch,
                    task=task_by_id[pair_key.task_manifest_id],
                    dependencies=dependencies,
                    complete_repair=complete,
                )
            proof_bindings.append(
                _proof_binding(
                    request=request,
                    receipt=receipt,
                    epoch=build_input.epoch,
                    dependencies=dependencies,
                )
            )
        return tuple(proof_bindings)

    return evaluate


def _release_files(project: Path, release_path: str) -> dict[str, bytes]:
    root = project / release_path
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


@pytest.fixture(scope="module")
def recorded_replay(tmp_path_factory):
    root = tmp_path_factory.mktemp("harness-replay-record")
    source_project = root / "source-project"
    build_input, _anchor_task, dependencies = _build_input(
        source_project,
        task_ids=("task.search.1", "task.search.2"),
    )
    evaluator_requests = []
    proposal_requests = []

    def proposals(request):
        proposal_requests.append(request)
        return _gain_proposals(index=0)(request)

    recorder = HarnessFactoryReplayRecorder(
        build_input=build_input,
        proposal_callback=proposals,
        evaluator_callback=_multitask_evaluator(
            build_input,
            dependencies,
            evaluator_requests,
        ),
    )
    source_result = build_harness_factory_release(
        build_input,
        proposal_callback=recorder.proposal_callback,
        evaluator_callback=recorder.evaluator_callback,
    )
    manifest = recorder.manifest(
        manifest_id="factory-replay.multitask",
    )
    manifest_path = write_harness_factory_replay_manifest(
        root / "factory-replay.json",
        manifest,
    )
    return {
        "root": root,
        "source_project": source_project,
        "build_input": build_input,
        "source_result": source_result,
        "manifest": manifest,
        "manifest_path": manifest_path,
        "proposal_requests": tuple(proposal_requests),
        "evaluator_requests": tuple(evaluator_requests),
    }


def test_multitask_transcript_publishes_byte_identical_release_without_callbacks(
    recorded_replay,
) -> None:
    target_project = recorded_replay["root"] / "target-project"
    target_input, _task, _dependencies = _build_input(
        target_project,
        task_ids=("task.search.1", "task.search.2"),
    )

    replayed = build_harness_factory_release_from_replay(
        target_input,
        replay_manifest_path=recorded_replay["manifest_path"],
    )

    source_result = recorded_replay["source_result"]
    assert replayed.service_result.search_result_digest == source_result.search_result_digest
    assert replayed.service_result.release_pointer.release_digest == source_result.release_pointer.release_digest
    assert replayed.service_result.selected_protocol_digest == source_result.selected_protocol_digest
    assert _release_files(
        target_project,
        replayed.service_result.release_pointer.release_path,
    ) == _release_files(
        recorded_replay["source_project"],
        source_result.release_pointer.release_path,
    )
    assert replayed.provenance.execution_mode == "deterministic_replay"
    assert replayed.provenance.live_inference_status == "not_run"
    assert replayed.provenance.real_inference_requests_sent == 0
    assert replayed.provenance.provider_invocation_receipt_digests == ()
    assert replayed.provenance.reconciliation_complete is True
    assert Path(replayed.provenance_path).is_file()
    assert load_harness_factory_replay_manifest(
        recorded_replay["manifest_path"],
    ) == recorded_replay["manifest"]


def test_callbacks_consume_exact_order_once_and_require_final_reconciliation(
    recorded_replay,
) -> None:
    manifest = recorded_replay["manifest"]
    proposal_request = recorded_replay["proposal_requests"][0]
    evaluator_requests = recorded_replay["evaluator_requests"]

    proposal = ReplayProposalCallback(manifest)
    assert proposal(proposal_request) == manifest.proposal_rows[0].proposals
    with pytest.raises(HarnessFactoryReplayExhaustedError, match="reused or extra"):
        proposal(proposal_request)

    out_of_order = ReplayEvaluatorCallback(manifest)
    with pytest.raises(HarnessFactoryReplayValidationError, match="order/identity"):
        out_of_order(evaluator_requests[1])
    evaluator = ReplayEvaluatorCallback(manifest)
    assert (
        evaluator(evaluator_requests[0])
        == manifest.evaluator_rows[0].proof_bindings
    )
    with pytest.raises(HarnessFactoryReplayValidationError, match="order/identity"):
        evaluator(evaluator_requests[0])

    untouched = HarnessFactoryReplayCallbacks(manifest)
    with pytest.raises(HarnessFactoryReplayIncompleteError, match="unconsumed"):
        untouched.assert_reconciled()


def test_tamper_order_missing_and_extra_rows_fail_before_release(
    recorded_replay,
) -> None:
    root = recorded_replay["root"]
    build_input = recorded_replay["build_input"]
    manifest = recorded_replay["manifest"]

    tampered_payload = json.loads(recorded_replay["manifest_path"].read_text(encoding="utf-8"))
    tampered_payload["evaluator_rows"][0]["proof_bindings"][0]["outcome_receipt"]["complete_repair"] = not tampered_payload[
        "evaluator_rows"
    ][0]["proof_bindings"][0]["outcome_receipt"]["complete_repair"]
    tampered_path = root / "tampered.json"
    tampered_path.write_text(
        json.dumps(tampered_payload, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(ValidationError, match="receipt_digest"):
        load_harness_factory_replay_manifest(tampered_path)

    missing_manifest = build_harness_factory_replay_manifest(
        manifest_id="factory-replay.missing",
        build_input=build_input,
        proposal_rows=manifest.proposal_rows,
        evaluator_rows=manifest.evaluator_rows[:-1],
    )
    missing_path = write_harness_factory_replay_manifest(
        root / "missing.json",
        missing_manifest,
    )
    missing_project = root / "missing-project"
    missing_input, _task, _deps = _build_input(
        missing_project,
        task_ids=("task.search.1", "task.search.2"),
    )
    with pytest.raises(PairedSearchIntegrityError, match="no remaining row"):
        build_harness_factory_release_from_replay(
            missing_input,
            replay_manifest_path=missing_path,
        )
    assert not (missing_project / "active_release.json").exists()

    first_request = recorded_replay["proposal_requests"][0]
    extra_request = replace(first_request, step_index=99)
    original_proposal = manifest.proposal_rows[0].proposals[0]
    extra_proposal = original_proposal.model_copy(
        update={"transaction_id": "txn.extra-replay-row"}
    )
    extra_row = ProposalReplayRow(
        sequence_no=len(manifest.proposal_rows),
        request=proposal_batch_request_identity(extra_request),
        request_digest=proposal_batch_request_digest(extra_request),
        proposals=(extra_proposal,),
    )
    extra_manifest = build_harness_factory_replay_manifest(
        manifest_id="factory-replay.extra",
        build_input=build_input,
        proposal_rows=(*manifest.proposal_rows, extra_row),
        evaluator_rows=manifest.evaluator_rows,
    )
    extra_path = write_harness_factory_replay_manifest(root / "extra.json", extra_manifest)
    extra_project = root / "extra-project"
    extra_input, _task, _deps = _build_input(
        extra_project,
        task_ids=("task.search.1", "task.search.2"),
    )
    with pytest.raises(HarnessFactoryReplayIncompleteError, match="unconsumed"):
        build_harness_factory_release_from_replay(
            extra_input,
            replay_manifest_path=extra_path,
        )
    assert not (extra_project / "active_release.json").exists()


def test_crossed_secret_sealed_and_noncanonical_manifests_are_rejected(
    recorded_replay,
) -> None:
    manifest = recorded_replay["manifest"]
    build_input = recorded_replay["build_input"]
    first = manifest.proposal_rows[0]
    secret_proposal = first.proposals[0].model_copy(
        update={
            "mechanism_hypothesis": (
                "Use sk-test-secret-value-1234567890 while replaying."
            )
        }
    )
    secret_row = ProposalReplayRow(
        sequence_no=0,
        request=first.request,
        request_digest=first.request_digest,
        proposals=(secret_proposal,),
    )
    with pytest.raises(
        (ValidationError, HarnessFactoryReplayValidationError),
        match="credential",
    ):
        build_harness_factory_replay_manifest(
            manifest_id="factory-replay.secret",
            build_input=build_input,
            proposal_rows=(secret_row,),
            evaluator_rows=manifest.evaluator_rows,
        )

    crossed_request = recorded_replay["evaluator_requests"][0]
    crossed = replace(crossed_request, opportunity_index=999)
    callback = ReplayEvaluatorCallback(manifest)
    with pytest.raises(HarnessFactoryReplayValidationError, match="order/identity"):
        callback(crossed)

    noncanonical = recorded_replay["root"] / "noncanonical.json"
    payload = json.loads(recorded_replay["manifest_path"].read_text(encoding="utf-8"))
    noncanonical.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    with pytest.raises(HarnessFactoryReplayValidationError, match="canonical immutable JSON"):
        load_harness_factory_replay_manifest(noncanonical)
