from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from pydantic import BaseModel


def _cli(*arguments: str, timeout: float = 240.0) -> subprocess.CompletedProcess[str]:
    environment = dict(os.environ)
    environment.pop("AGINTOR_PROCESS_ROLE", None)
    return subprocess.run(
        [sys.executable, "-m", "agintor.cli_v1", *arguments],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        shell=False,
        timeout=timeout,
        check=False,
    )


def _write_json(path: Path, payload: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return path


def _model_json(value: object) -> object:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    return value


def _controlled_fixture(root: Path):
    from agintor.evaluation.pilot import (
        CONTROLLED_EVIDENCE_DIR,
        normalize_mvp_evidence_artifacts,
    )
    from tests.mvp.test_p1_pilot_evidence import (
        CANARY,
        _core,
        _full_evaluation_contract,
        _packet_fixture,
    )

    packet, artifact_values = _packet_fixture()
    normalized = normalize_mvp_evidence_artifacts(artifact_values)
    artifact_sources = []
    for index, (packet_path, raw) in enumerate(sorted(normalized.items())):
        source = root / "inputs" / "artifacts" / f"{index:04d}.bin"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(raw)
        artifact_sources.append(
            {
                "packet_path": packet_path,
                "source_path": source.relative_to(root).as_posix(),
            }
        )

    _dependencies, epoch, task, _protocol, _plan = _core()
    contract = _full_evaluation_contract(epoch, task)
    contract_path = _write_json(
        root / "inputs" / "sealed_evaluation_contract.json",
        contract.model_dump(mode="json"),
    )
    build_request = {
        "schema_version": "repo-repair-harness-readiness-entry-request-v1",
        "operation": "build",
        "packet_id": packet.packet_id,
        "destination_root": "packets",
        "evaluation_contract_source_path": contract_path.relative_to(root).as_posix(),
        "artifacts": artifact_sources,
        "release": packet.release.model_dump(mode="json"),
        "gate0": packet.gate0.model_dump(mode="json"),
        "d0": packet.d0.model_dump(mode="json"),
        "s1": packet.s1.model_dump(mode="json"),
        "solve_execution": packet.solve_execution.model_dump(mode="json"),
        "task_audit": _model_json(
            artifact_values[
                f"{CONTROLLED_EVIDENCE_DIR}/evaluator/task_audit_manifest.json"
            ]
        ),
        "pilot_dry_run": _model_json(
            artifact_values[f"{CONTROLLED_EVIDENCE_DIR}/pilot/dry_run_manifest.json"]
        ),
        "pilot_report": _model_json(
            artifact_values[f"{CONTROLLED_EVIDENCE_DIR}/analysis/pilot_report.json"]
        ),
        "factory_followup": packet.factory_followup.model_dump(mode="json"),
        "runtime_sessions": packet.runtime_sessions.model_dump(mode="json"),
        "limitations": list(packet.limitations),
    }
    request_path = _write_json(root / "requests" / "build.json", build_request)
    return packet, artifact_values, build_request, request_path, contract_path, CANARY


def test_readiness_commands_use_fresh_evaluator_child_and_replay_immutable_packet(
    tmp_path: Path,
) -> None:
    imported = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import agintor.cli_v1; "
                "assert 'agintor.evaluation.pilot' not in sys.modules; "
                "assert 'agintor.evaluation.contracts' not in sys.modules; "
                "import agintor.evaluation.readiness_entrypoint; "
                "assert 'agintor.evaluation.pilot' not in sys.modules; "
                "assert 'agintor.evaluation.contracts' not in sys.modules"
            ),
        ],
        cwd=Path(__file__).resolve().parents[2],
        capture_output=True,
        text=True,
        check=False,
    )
    assert imported.returncode == 0, imported.stderr

    controlled_root = (tmp_path / "controlled-readiness").resolve()
    controlled_root.mkdir()
    packet, _artifacts, _request, build_path, contract_path, canary = (
        _controlled_fixture(controlled_root)
    )
    sentinel = "READINESS_PARENT_CREDENTIAL_SENTINEL_MUST_NOT_CROSS"
    environment_value = os.environ.get("OPENAI_API_KEY")
    os.environ["OPENAI_API_KEY"] = sentinel
    try:
        built = _cli(
            "readiness-build",
            str(controlled_root),
            "--request-json",
            str(build_path),
        )
    finally:
        if environment_value is None:
            os.environ.pop("OPENAI_API_KEY", None)
        else:
            os.environ["OPENAI_API_KEY"] = environment_value

    assert built.returncode == 0, built.stderr or built.stdout
    public = json.loads(built.stdout)
    assert set(public) == {
        "schema_version",
        "status",
        "operation",
        "live_status",
        "real_inference_requests_sent",
        "packet_id",
        "packet_digest",
        "packet_path",
    }
    assert public == {
        "schema_version": "repo-repair-harness-cli-readiness-result-v1",
        "status": "succeeded",
        "operation": "readiness-build",
        "live_status": "not_run",
        "real_inference_requests_sent": 0,
        "packet_id": packet.packet_id,
        "packet_digest": packet.packet_digest,
        "packet_path": f"packets/{packet.packet_digest}",
    }
    assert str(controlled_root) not in built.stdout
    assert sentinel not in built.stdout
    assert canary not in built.stdout

    generation = controlled_root / public["packet_path"]
    manifest_path = generation / "mvp_readiness_packet.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["packet_digest"] == packet.packet_digest
    assert manifest["capability_claim_authorized"] is False
    assert manifest["live_gate0_status"] == "not_run"
    assert manifest["live_pilot_status"] == "not_run"
    assert manifest["inference_requests_sent"] == 0
    assert not (generation / contract_path.relative_to(controlled_root)).exists()

    replay_path = _write_json(
        controlled_root / "requests" / "replay.json",
        {
            "schema_version": "repo-repair-harness-readiness-entry-request-v1",
            "operation": "replay",
            "generation_path": public["packet_path"],
            "evaluation_contract_source_path": contract_path.relative_to(
                controlled_root
            ).as_posix(),
        },
    )
    replayed = _cli(
        "readiness-replay",
        str(controlled_root),
        "--request-json",
        str(replay_path),
    )

    assert replayed.returncode == 0, replayed.stderr or replayed.stdout
    replay_public = json.loads(replayed.stdout)
    assert replay_public == {
        **public,
        "operation": "readiness-replay",
    }
    assert str(controlled_root) not in replayed.stdout
    assert canary not in replayed.stdout

    crossed_contract_payload = json.loads(contract_path.read_text(encoding="utf-8"))
    crossed_contract_payload.pop("evaluation_contract_digest", None)
    crossed_contract_payload["canaries"][0]["value"] = "CROSSED-READINESS-CANARY"
    crossed_contract_payload["canaries"][0].pop("value_digest", None)
    crossed_contract_path = _write_json(
        controlled_root / "inputs" / "crossed_evaluation_contract.json",
        crossed_contract_payload,
    )
    crossed_replay_path = _write_json(
        controlled_root / "requests" / "crossed-replay.json",
        {
            "schema_version": "repo-repair-harness-readiness-entry-request-v1",
            "operation": "replay",
            "generation_path": public["packet_path"],
            "evaluation_contract_source_path": crossed_contract_path.relative_to(
                controlled_root
            ).as_posix(),
        },
    )
    crossed = _cli(
        "readiness-replay",
        str(controlled_root),
        "--request-json",
        str(crossed_replay_path),
    )
    assert crossed.returncode == 2
    crossed_payload = json.loads(crossed.stdout)
    assert crossed_payload["code"] == "evaluation_contract_identity_mismatch"
    assert "CROSSED-READINESS-CANARY" not in crossed.stdout
    assert str(controlled_root) not in crossed.stdout

    limitations = generation / "public_release_evidence" / "limitations.md"
    limitations.write_text(
        limitations.read_text(encoding="utf-8") + "tampered\n",
        encoding="utf-8",
        newline="\n",
    )
    rejected = _cli(
        "readiness-replay",
        str(controlled_root),
        "--request-json",
        str(replay_path),
    )
    assert rejected.returncode == 2
    rejection = json.loads(rejected.stdout)
    assert rejection["code"] == "readiness_replay_failed"
    assert str(controlled_root) not in rejected.stdout
    assert canary not in rejected.stdout


def test_readiness_parent_rejects_request_outside_controlled_root(
    tmp_path: Path,
) -> None:
    controlled_root = (tmp_path / "controlled").resolve()
    controlled_root.mkdir()
    request_payload = {
        "schema_version": "repo-repair-harness-readiness-entry-request-v1",
        "operation": "replay",
        "generation_path": "packets/" + "0" * 64,
        "evaluation_contract_source_path": "sealed/evaluation_contract.json",
    }
    outside = _write_json(tmp_path / "outside.json", request_payload)

    rejected = _cli(
        "readiness-replay",
        str(controlled_root),
        "--request-json",
        str(outside),
    )

    assert rejected.returncode == 2
    payload = json.loads(rejected.stdout)
    assert payload["code"] == "readiness_request_path_invalid"
    assert not (controlled_root / "packets").exists()

    inside = _write_json(controlled_root / "requests" / "replay.json", request_payload)
    child_output = tmp_path / "child-output.json"
    environment = dict(os.environ)
    environment["AGINTOR_PROCESS_ROLE"] = "factory"
    child = subprocess.run(
        [
            sys.executable,
            "-m",
            "agintor.evaluation.readiness_entrypoint",
            "--controlled-root",
            str(controlled_root),
            "--request-json",
            str(inside),
            "--output-json",
            str(child_output),
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        shell=False,
        timeout=60.0,
        check=False,
    )
    assert child.returncode == 2
    child_payload = json.loads(child_output.read_text(encoding="utf-8"))
    assert child_payload["code"] == "evaluator_role_required"
    assert not (controlled_root / "packets").exists()
