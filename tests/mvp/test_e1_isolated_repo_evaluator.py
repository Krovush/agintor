from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

import pytest

from agintor.evaluation.runners.repo_patch_backends import (
    IsolatedRepoPatchCommandBackend,
    TrustedLocalRepoPatchCommandBackend,
)
from agintor.evaluation.runners.repo_patch_runner import (
    RepoPatchCommand,
    RepoPatchEvaluatorRunner,
    RepoPatchFixture,
    repo_patch_fixture_digest,
    repo_snapshot_digest,
)
from agintor.isolation.commands import IsolatedCommandRequest, IsolatedCommandResult, IsolatedCommandStatus


_REPLAY_PATCHED_DIGEST = "c" * 64
_REPLAY_DRIFT_DIGEST = "d" * 64


class RecordingIsolatedBackend:
    """Offline containment stand-in; records requests and executes in scratch."""

    def __init__(self, *, source_to_tamper: Path | None = None) -> None:
        self.requests: list[IsolatedCommandRequest] = []
        self._delegate = TrustedLocalRepoPatchCommandBackend()
        self._source_to_tamper = source_to_tamper

    def run(self, request: IsolatedCommandRequest) -> IsolatedCommandResult:
        self.requests.append(request)
        if self._source_to_tamper is not None and len(self.requests) == 1:
            self._source_to_tamper.write_text("VALUE = 999\n", encoding="utf-8")
        return self._delegate.run(request)


class VirtualReplayTransitionBackend:
    """Replay stand-in whose authoritative transition is not the physical tree."""

    def __init__(self, *, drift_sequence_no: int) -> None:
        self.requests: list[IsolatedCommandRequest] = []
        self.drift_sequence_no = drift_sequence_no
        self._last_workspace_transition: dict[str, str] | None = None

    @property
    def last_workspace_transition(self) -> dict[str, str] | None:
        if self._last_workspace_transition is None:
            return None
        return dict(self._last_workspace_transition)

    def run(self, request: IsolatedCommandRequest) -> IsolatedCommandResult:
        sequence_no = len(self.requests)
        self.requests.append(request)
        before = repo_snapshot_digest(request.workspace) if sequence_no == 0 else _REPLAY_PATCHED_DIGEST
        after = _REPLAY_DRIFT_DIGEST if sequence_no == self.drift_sequence_no else _REPLAY_PATCHED_DIGEST
        self._last_workspace_transition = {
            "source": "replay_manifest",
            "before": before,
            "after": after,
        }
        empty_digest = hashlib.sha256(b"").hexdigest()
        return IsolatedCommandResult(
            status=IsolatedCommandStatus.COMPLETED,
            command=request.command,
            container_name=f"virtual-replay-{sequence_no}",
            exit_code=0,
            stdout="",
            stderr="",
            stdout_digest=empty_digest,
            stderr_digest=empty_digest,
            duration_s=0.0,
        )


def _backend(
    recorder: RecordingIsolatedBackend | None = None,
) -> tuple[IsolatedRepoPatchCommandBackend, RecordingIsolatedBackend]:
    command_backend = recorder or RecordingIsolatedBackend()
    backend = IsolatedRepoPatchCommandBackend(
        command_backend,
        environment_identity={
            "image": f"agintor-repair@sha256:{'a' * 64}",
            "network": "none",
            "user": "65532:65532",
        },
    )
    return backend, command_backend


def _write_source(root: Path) -> None:
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir(parents=True)
    (root / "src" / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "tests" / "test_public.py").write_text("PUBLIC_SENTINEL = True\n", encoding="utf-8")


def _command(name: str, assertion: str) -> RepoPatchCommand:
    return RepoPatchCommand(
        name=name,
        command=[
            "python",
            "-c",
            (
                "from pathlib import Path\n"
                "text = Path('src/app.py').read_text(encoding='utf-8')\n"
                f"assert {assertion}, text\n"
            ),
        ],
        timeout_s=5.0,
    )


def _mutating_command(name: str, replacement: str) -> RepoPatchCommand:
    return RepoPatchCommand(
        name=name,
        command=[
            "python",
            "-c",
            (
                "from pathlib import Path\n"
                "path = Path('src/app.py')\n"
                "text = path.read_text(encoding='utf-8')\n"
                "assert 'VALUE = 2' in text, text\n"
                f"path.write_text({replacement!r}, encoding='utf-8')\n"
            ),
        ],
        timeout_s=5.0,
    )


def _fixture(source: Path, backend: IsolatedRepoPatchCommandBackend) -> RepoPatchFixture:
    fixture = RepoPatchFixture(
        repo_snapshot_path=str(source),
        expected_repo_snapshot_digest=repo_snapshot_digest(source),
        evaluation_contract_digest="b" * 64,
        public_test_commands=[_command("public", "'VALUE = 2' in text")],
        sealed_test_commands=[_command("sealed", "text.strip() == 'VALUE = 2'")],
        protected_paths=["tests"],
        timeout_s=5.0,
    )
    return fixture.model_copy(
        update={"authority_fixture_digest": repo_patch_fixture_digest(fixture, backend)}
    )


def _rebind_fixture_authority(
    fixture: RepoPatchFixture,
    backend: IsolatedRepoPatchCommandBackend,
) -> RepoPatchFixture:
    return fixture.model_copy(
        update={"authority_fixture_digest": repo_patch_fixture_digest(fixture, backend)}
    )


def _artifact(content: str = "VALUE = 2\n") -> dict[str, object]:
    return {"files": [{"path": "src/app.py", "updated_content": content}]}


def test_known_good_patch_uses_one_backend_for_apply_public_and_sealed_commands(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_source(source)
    backend, recorder = _backend()
    fixture = _fixture(source, backend)
    original_digest = repo_snapshot_digest(source)

    result = RepoPatchEvaluatorRunner(backend).run(
        candidate_artifact={
            **_artifact(),
            "applied": False,
            "public_tests_passed": False,
            "hidden_tests_passed": False,
        },
        fixture=fixture,
    )

    assert result.status == "pass"
    assert result.complete_repair is True
    assert result.applied is True
    assert result.public_tests_passed is True
    assert result.hidden_tests_passed is True
    assert result.fixture_identity_matched is True
    assert result.source_snapshot_unchanged is True
    assert result.scratch_snapshot_matched is True
    assert result.clean_copy_snapshot_unchanged is True
    assert result.patched_clean_digest
    assert repo_snapshot_digest(source) == original_digest
    assert (source / "src" / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"
    assert len(recorder.requests) == 3
    assert len({request.workspace for request in recorder.requests}) == 1
    assert all(request.working_directory == "repo" for request in recorder.requests)
    assert all(isinstance(request.command, tuple) and request.command for request in recorder.requests)
    assert result.hidden_command_results[0].command == ["<sealed-command>"]
    assert result.hidden_command_results[0].stdout == ""
    assert result.outcome_health(no_leakage=True, accounting_complete=True).passes_promotion_floor
    receipt_evidence = result.outcome_receipt_evidence(no_leakage=True, accounting_complete=True)
    assert receipt_evidence["evaluation_contract_digest"] == "b" * 64
    assert receipt_evidence["environment_digest"] == result.environment_digest
    assert receipt_evidence["patch_digest"] == result.patch_digest
    assert receipt_evidence["complete_repair"] is True


@pytest.mark.skipif(os.name != "posix", reason="POSIX container-user mode boundary")
def test_evaluator_mount_copy_is_nonroot_accessible_but_source_stays_private(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _write_source(source)
    source.chmod(0o700)
    (source / "src").chmod(0o700)
    (source / "src" / "app.py").chmod(0o600)
    source_mode = stat.S_IMODE(source.stat().st_mode)
    source_dir_mode = stat.S_IMODE((source / "src").stat().st_mode)
    source_file_mode = stat.S_IMODE((source / "src" / "app.py").stat().st_mode)
    observed: list[Path] = []

    class PermissionCheckingBackend(RecordingIsolatedBackend):
        def run(self, request: IsolatedCommandRequest) -> IsolatedCommandResult:
            observed.append(request.workspace)
            assert stat.S_IMODE(request.workspace.parent.stat().st_mode) == 0o700
            assert stat.S_IMODE(request.workspace.stat().st_mode) == 0o777
            assert stat.S_IMODE((request.workspace / "repo" / "src").stat().st_mode) == 0o777
            assert stat.S_IMODE(
                (request.workspace / "repo" / "src" / "app.py").stat().st_mode
            ) == 0o666
            return super().run(request)

    recorder = PermissionCheckingBackend()
    backend, _ = _backend(recorder)

    result = RepoPatchEvaluatorRunner(backend).run(
        candidate_artifact=_artifact(),
        fixture=_fixture(source, backend),
    )

    assert result.complete_repair is True
    assert len(observed) == 3
    assert all(not workspace.exists() for workspace in observed)
    assert stat.S_IMODE(source.stat().st_mode) == source_mode
    assert stat.S_IMODE((source / "src").stat().st_mode) == source_dir_mode
    assert stat.S_IMODE((source / "src" / "app.py").stat().st_mode) == source_file_mode


def test_empty_candidate_fails_without_launching_candidate_commands(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_source(source)
    backend, recorder = _backend()

    result = RepoPatchEvaluatorRunner(backend).run(candidate_artifact={}, fixture=_fixture(source, backend))

    assert result.status == "fail"
    assert result.applied is False
    assert result.complete_repair is False
    assert recorder.requests == []
    assert repo_snapshot_digest(source) == result.repo_snapshot_digest


@pytest.mark.parametrize("path", ["../escape.py", "C:/host/escape.py", "/host/escape.py"])
def test_traversal_and_host_absolute_candidates_fail_before_backend_launch(
    tmp_path: Path,
    path: str,
) -> None:
    source = tmp_path / "source"
    _write_source(source)
    backend, recorder = _backend()

    result = RepoPatchEvaluatorRunner(backend).run(
        candidate_artifact={"files": [{"path": path, "updated_content": "ESCAPED = True\n"}]},
        fixture=_fixture(source, backend),
    )

    assert result.status == "fail"
    assert result.applied is False
    assert "stay within" in result.patch_apply.stderr
    assert recorder.requests == []
    assert not (tmp_path / "escape.py").exists()


def test_protected_path_candidate_is_quarantined_without_running_modified_checks(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_source(source)
    backend, recorder = _backend()

    result = RepoPatchEvaluatorRunner(backend).run(
        candidate_artifact={
            "files": [
                {"path": "src/app.py", "updated_content": "VALUE = 2\n"},
                {"path": "tests/test_public.py", "updated_content": "PUBLIC_SENTINEL = False\n"},
            ]
        },
        fixture=_fixture(source, backend),
    )

    assert result.status == "quarantine"
    assert result.applied is False
    assert result.tampered_tests is True
    assert result.tampered_paths == ["tests"]
    assert recorder.requests == []
    assert (source / "tests" / "test_public.py").read_text(encoding="utf-8") == "PUBLIC_SENTINEL = True\n"


def test_plausible_wrong_patch_passes_public_but_fails_sealed_check(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_source(source)
    backend, recorder = _backend()

    result = RepoPatchEvaluatorRunner(backend).run(
        candidate_artifact=_artifact("VALUE = 2  # plausible but not exact\n"),
        fixture=_fixture(source, backend),
    )

    assert result.status == "fail"
    assert result.applied is True
    assert result.public_tests_passed is True
    assert result.hidden_tests_passed is False
    assert result.complete_repair is False
    assert len(recorder.requests) == 3


def test_passing_public_check_that_mutates_source_is_quarantined(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _write_source(source)
    backend, recorder = _backend()
    fixture = _fixture(source, backend).model_copy(
        update={
            "public_test_commands": [_mutating_command("public-mutates-src", "VALUE = 3\n")]
        }
    )
    fixture = _rebind_fixture_authority(fixture, backend)

    result = RepoPatchEvaluatorRunner(backend).run(
        candidate_artifact=_artifact(),
        fixture=fixture,
    )

    assert result.status == "quarantine"
    assert result.applied is True
    assert result.public_tests_passed is True
    assert result.hidden_tests_passed is False
    assert result.clean_copy_snapshot_unchanged is False
    assert result.tampered_tests is True
    assert result.complete_repair is False
    assert len(recorder.requests) == 2
    assert result.hidden_command_results == []
    drift = [item for item in result.workspace_drift_evidence if item.phase == "public_check"]
    assert len(drift) == 1
    assert drift[0].matched is False
    assert drift[0].expected_digest == result.patched_clean_digest
    assert drift[0].before_digest == result.patched_clean_digest
    assert drift[0].after_digest != result.patched_clean_digest
    assert drift[0].digest_source == "physical_clean_repo"
    assert (source / "src" / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_passing_sealed_check_that_mutates_source_is_quarantined(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    _write_source(source)
    backend, recorder = _backend()
    fixture = _fixture(source, backend).model_copy(
        update={
            "sealed_test_commands": [_mutating_command("sealed-mutates-src", "VALUE = 4\n")]
        }
    )
    fixture = _rebind_fixture_authority(fixture, backend)

    result = RepoPatchEvaluatorRunner(backend).run(
        candidate_artifact=_artifact(),
        fixture=fixture,
    )

    assert result.status == "quarantine"
    assert result.applied is True
    assert result.public_tests_passed is True
    assert result.hidden_tests_passed is True
    assert result.clean_copy_snapshot_unchanged is False
    assert result.tampered_tests is True
    assert result.complete_repair is False
    assert len(recorder.requests) == 3
    drift = [item for item in result.workspace_drift_evidence if item.phase == "sealed_check"]
    assert len(drift) == 1
    assert drift[0].matched is False
    assert drift[0].expected_digest == result.patched_clean_digest
    assert drift[0].before_digest == result.patched_clean_digest
    assert drift[0].after_digest != result.patched_clean_digest
    assert drift[0].digest_source == "physical_clean_repo"
    assert result.hidden_command_results[0].command == ["<sealed-command>"]
    assert (source / "src" / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"


@pytest.mark.parametrize(
    ("drift_sequence_no", "phase", "expected_requests", "hidden_passed"),
    [
        (1, "public_check", 2, False),
        (2, "sealed_check", 3, True),
    ],
)
def test_replay_manifest_workspace_drift_is_quarantined_without_physical_mutation(
    tmp_path: Path,
    drift_sequence_no: int,
    phase: str,
    expected_requests: int,
    hidden_passed: bool,
) -> None:
    source = tmp_path / "source"
    _write_source(source)
    recorder = VirtualReplayTransitionBackend(drift_sequence_no=drift_sequence_no)
    backend = IsolatedRepoPatchCommandBackend(
        recorder,
        environment_identity={
            "image": f"agintor-repair@sha256:{'a' * 64}",
            "network": "none",
            "user": "65532:65532",
        },
    )
    fixture = _fixture(source, backend)

    result = RepoPatchEvaluatorRunner(backend).run(
        candidate_artifact=_artifact(),
        fixture=fixture,
    )

    assert result.status == "quarantine"
    assert result.clean_copy_snapshot_unchanged is False
    assert result.tampered_tests is True
    assert result.complete_repair is False
    assert result.hidden_tests_passed is hidden_passed
    assert len(recorder.requests) == expected_requests
    drift = [item for item in result.workspace_drift_evidence if item.phase == phase]
    assert len(drift) == 1
    assert drift[0].digest_source == "backend_recorded_workspace"
    assert drift[0].expected_digest == _REPLAY_PATCHED_DIGEST
    assert drift[0].before_digest == _REPLAY_PATCHED_DIGEST
    assert drift[0].after_digest == _REPLAY_DRIFT_DIGEST
    assert (source / "src" / "app.py").read_text(encoding="utf-8") == "VALUE = 1\n"


def test_original_snapshot_tampering_is_detected_and_stops_evaluation(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_source(source)
    recorder = RecordingIsolatedBackend(source_to_tamper=source / "src" / "app.py")
    backend, recorder = _backend(recorder)

    result = RepoPatchEvaluatorRunner(backend).run(
        candidate_artifact=_artifact(),
        fixture=_fixture(source, backend),
    )

    assert result.status == "quarantine"
    assert result.source_snapshot_unchanged is False
    assert result.complete_repair is False
    assert len(recorder.requests) == 1
    assert result.public_command_results == []
    assert result.hidden_command_results == []


def test_isolated_mode_rejects_shell_strings_and_never_falls_back_to_local_execution(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_source(source)
    backend, recorder = _backend()
    fixture = RepoPatchFixture(
        repo_snapshot_path=str(source),
        public_test_commands=[RepoPatchCommand(name="shell", command="python -c \"print('no')\"")],
        protected_paths=["tests"],
    )

    with pytest.raises(ValueError, match="shell strings are forbidden"):
        RepoPatchEvaluatorRunner(backend).run(candidate_artifact=_artifact(), fixture=fixture)

    assert recorder.requests == []


def test_fixture_manifest_binds_source_checks_protection_environment_and_backend(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_source(source)
    backend, recorder = _backend()
    fixture = _fixture(source, backend)
    mismatched = fixture.model_copy(update={"authority_fixture_digest": "0" * 64})

    result = RepoPatchEvaluatorRunner(backend).run(candidate_artifact=_artifact(), fixture=mismatched)

    assert result.status == "quarantine"
    assert result.fixture_identity_matched is False
    assert result.applied is False
    assert recorder.requests == []
    assert result.outcome_health(no_leakage=True, accounting_complete=True).environment_integrity is False
