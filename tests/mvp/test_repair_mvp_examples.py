from __future__ import annotations

import os
import shutil
import subprocess
import sys
import venv
from pathlib import Path


EXPECTED_EXAMPLE_FILES = {
    "deployment-profile.json",
    "deployment-profile-openai-luna.json",
    "deployment-profile-openai-terra.json",
    "epoch-public.json",
    "gate0-dry-run-request.json",
    "public-task.json",
    "readiness-replay-request.json",
    "solve-pair-key.json",
    "solve-replay-file-request.json",
    "workflow-references.json",
}


def _validation_script() -> str:
    expected = sorted(EXPECTED_EXAMPLE_FILES)
    return f"""
import json
import re
from importlib.resources import files
from pathlib import PurePosixPath

from agintor.authority.public_tasks import assert_public_payload
from agintor.contracts.epochs import TaskEnvelope
from agintor.contracts.outcomes import PairKey
from agintor.contracts.run_evidence import assert_no_resolved_credentials
from agintor.evaluation.gate0 import (
    build_gate0_dry_run_manifest,
    build_gate0_provider_identity,
    validate_gate0_dry_run_conformance,
)
from agintor.evaluation.readiness_entrypoint import ReadinessReplayEnvelope
from agintor.runtime.harness_profile import HarnessDeploymentProfile
from agintor.runtime.sdk.harness_entrypoint import HarnessSolveFileRequest


expected = set({expected!r})
root = files("agintor.examples.repair_mvp")
payloads = {{
    path.name: json.loads(path.read_text(encoding="utf-8"))
    for path in root.iterdir()
    if path.name.endswith(".json")
}}
if set(payloads) != expected:
    raise AssertionError({{"missing_or_extra": sorted(set(payloads) ^ expected)}})

FORBIDDEN_KEY_NAMES = {{
    "answer_key",
    "canary_id",
    "evaluation_contract",
    "evaluation_contract_digest",
    "expected_answer",
    "expected_output",
    "hidden_checks",
    "hidden_tests",
    "outcome_authority",
    "protected_paths",
    "run_evidence",
    "sealed_fixture",
}}
FORBIDDEN_KEY_PREFIXES = (
    "private_",
    "sealed_",
    "hidden_",
    "oracle_private_",
    "gold_",
)
FORBIDDEN_VALUE_FRAGMENTS = (
    "p1-sealed-canary",
    "tests/hidden",
    "/hidden",
    "hidden/",
    "\\\\hidden",
    "/sealed",
    "sealed/",
    "\\\\sealed",
    "private_a_only",
    "private_b_only",
    "private_evidence",
    "expected_answer",
    "hidden_checks",
    "protected_paths",
    "sealed_fixture",
    "outcome_authority",
)


def normalized_key(value):
    return re.sub(r"[^a-z0-9]+", "_", str(value).casefold()).strip("_")


def assert_no_forbidden_authority(value, path="<root>"):
    if isinstance(value, dict):
        for raw_key, item in value.items():
            key = normalized_key(raw_key)
            child_path = f"{{path}}.{{raw_key}}"
            if key in FORBIDDEN_KEY_NAMES or key.startswith(FORBIDDEN_KEY_PREFIXES):
                raise AssertionError(f"forbidden controlled-authority key at {{child_path}}")
            assert_no_forbidden_authority(item, child_path)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            assert_no_forbidden_authority(item, f"{{path}}[{{index}}]")
        return
    if isinstance(value, str):
        text = value.casefold().replace("\\\\", "/")
        if any(fragment in text for fragment in FORBIDDEN_VALUE_FRAGMENTS):
            raise AssertionError(f"forbidden controlled-authority value at {{path}}: {{value!r}}")


def walk_no_live_state(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {{"real_inference_requests_sent", "inference_requests_sent"}} and item != 0:
                raise AssertionError(f"example contains sent inference count: {{key}}={{item!r}}")
            if key in {{"live_status", "live_inference_status", "real_provider_baseline_status"}}:
                if item != "not_run":
                    raise AssertionError(f"example contains non-not_run live status: {{key}}={{item!r}}")
            walk_no_live_state(item)
    elif isinstance(value, list):
        for item in value:
            walk_no_live_state(item)


for name, payload in payloads.items():
    assert_no_resolved_credentials(payload)
    assert_public_payload(payload)
    assert_no_forbidden_authority(payload, name)
    walk_no_live_state(payload)

profile = HarnessDeploymentProfile.model_validate(payloads["deployment-profile.json"])
gate0_request = payloads["gate0-dry-run-request.json"]
if set(gate0_request) != {{
    "schema_version",
    "deployment_profile",
    "provider_evidence_destination",
    "manifest_destination",
}}:
    raise AssertionError("Gate0 request key set drifted")
if gate0_request["schema_version"] != "repo-repair-harness-cli-gate0-dry-run-request-v1":
    raise AssertionError("Gate0 request schema drifted")
if HarnessDeploymentProfile.model_validate(gate0_request["deployment_profile"]) != profile:
    raise AssertionError("Gate0 request crossed the standalone deployment profile")
gate0_manifest = build_gate0_dry_run_manifest(
    provider_identity=build_gate0_provider_identity(deployment_profile=profile),
    evidence_destination=gate0_request["provider_evidence_destination"],
)
gate0_conformance = validate_gate0_dry_run_conformance(gate0_manifest)
if not gate0_conformance.passed or gate0_manifest.live_status != "not_run":
    raise AssertionError("Gate0 example is not deterministic no-live conformant")

epoch_public = payloads["epoch-public.json"]
if set(epoch_public) != {{
    "runtime_contract_version",
    "epoch_id",
    "epoch_manifest_digest",
    "capability_epoch",
    "development_split_digest",
    "deployment",
    "per_run_ceilings",
    "trusted_tools",
    "mutation_surface",
}}:
    raise AssertionError("public epoch projection key set drifted")

public_task = TaskEnvelope.model_validate(payloads["public-task.json"])
if public_task.epoch_manifest_digest != epoch_public["epoch_manifest_digest"]:
    raise AssertionError("public task crossed public epoch identity")
if public_task.split_manifest_digest != epoch_public["development_split_digest"]:
    raise AssertionError("public task crossed development split identity")

pair_key = PairKey.model_validate(payloads["solve-pair-key.json"])
solve_request = HarnessSolveFileRequest.model_validate(
    payloads["solve-replay-file-request.json"]
)
if solve_request.execution.mode != "replay":
    raise AssertionError("solve example must stay deterministic replay")
if solve_request.task != public_task or solve_request.pair_key != pair_key:
    raise AssertionError("solve request crossed public task or PairKey example")
if pair_key.task_manifest_id != public_task.task_manifest_id:
    raise AssertionError("PairKey crossed public task identity")

readiness_replay = ReadinessReplayEnvelope.model_validate(
    payloads["readiness-replay-request.json"]
)
if readiness_replay.operation != "replay":
    raise AssertionError("readiness replay request crossed operation")
for controlled_path in (
    readiness_replay.generation_path,
    readiness_replay.evaluation_contract_source_path,
):
    parts = {{part.casefold() for part in PurePosixPath(controlled_path).parts}}
    if parts & {{"hidden", "sealed", "sealed_fixture", "sealed_fixtures"}}:
        raise AssertionError("readiness replay path references forbidden controlled directories")

workflow = payloads["workflow-references.json"]
if workflow["schema_version"] != "repo-repair-harness-public-workflow-references-v1":
    raise AssertionError("workflow reference schema drifted")
referenced_inputs = set(workflow["public_inputs"]) | set(workflow["path_reference_requests"])
if not referenced_inputs <= expected:
    raise AssertionError("workflow references unknown packaged files")
for generated_request in workflow["product_generated_controlled_requests"]:
    path_name = PurePosixPath(generated_request["generated_request_path"]).name
    if path_name in payloads:
        raise AssertionError("product-generated controlled request was packaged as an example")
    if not set(generated_request["public_inputs"]) <= expected:
        raise AssertionError("controlled request references unknown public inputs")
    if "argv" in generated_request or "command" in generated_request:
        raise AssertionError("public examples must not ship a standalone proof workflow")

print(json.dumps({{"validated": sorted(payloads)}}))
"""


def _clean_env() -> dict[str, str]:
    environment = dict(os.environ)
    environment.pop("AGINTOR_PROCESS_ROLE", None)
    environment.pop("PYTHONPATH", None)
    return environment


def _remove_stale_example_build_cache(repo_root: Path) -> None:
    build_root = (repo_root / "build").resolve()
    example_cache = build_root / "lib" / "agintor" / "examples" / "repair_mvp"
    if not example_cache.exists():
        return

    example_cache.relative_to(build_root)
    shutil.rmtree(example_cache)


def test_repair_mvp_examples_validate_from_source_package() -> None:
    completed = subprocess.run(
        [sys.executable, "-c", _validation_script()],
        cwd=Path(__file__).resolve().parents[2],
        env=_clean_env(),
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_repair_mvp_examples_are_packaged_in_built_wheel(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    _remove_stale_example_build_cache(repo_root)
    wheelhouse = tmp_path / "wheelhouse"
    wheelhouse.mkdir()
    built = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            ".",
            "--no-deps",
            "--wheel-dir",
            str(wheelhouse),
        ],
        cwd=repo_root,
        env=_clean_env(),
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    assert built.returncode == 0, built.stderr or built.stdout
    wheels = sorted(wheelhouse.glob("agintor-*.whl"))
    assert wheels

    venv_root = tmp_path / "venv"
    venv.EnvBuilder(with_pip=True, system_site_packages=True).create(venv_root)
    python = venv_root / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    installed = subprocess.run(
        [str(python), "-m", "pip", "install", "--no-deps", str(wheels[-1])],
        cwd=tmp_path,
        env=_clean_env(),
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert installed.returncode == 0, installed.stderr or installed.stdout

    validated = subprocess.run(
        [str(python), "-c", _validation_script()],
        cwd=tmp_path,
        env=_clean_env(),
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

    assert validated.returncode == 0, validated.stderr or validated.stdout
