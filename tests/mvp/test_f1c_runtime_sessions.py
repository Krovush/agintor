from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from agintor.storage.harness_session_store import (
    HARNESS_RUNTIME_KIND,
    HARNESS_SESSIONS_DIR_NAME,
    HarnessSessionConcurrencyError,
    HarnessSessionLimits,
    HarnessSessionReleaseMismatchError,
    HarnessSessionStore,
    HarnessSessionValidationError,
    HarnessSessionVersionError,
)


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _entry(
    ref: str = "artifacts/public-result.json",
    *,
    digest_label: str = "artifact",
    summary: str = "Public-safe concise result summary.",
) -> dict[str, str]:
    return {
        "artifact_ref": ref,
        "artifact_digest": _digest(digest_label),
        "summary": summary,
    }


def test_sessions_live_outside_releases_and_same_release_context_is_declared_only(tmp_path: Path) -> None:
    project = tmp_path / "factory-project"
    release = _digest("release-one")
    store = HarnessSessionStore(project)

    first = store.create_session(active_release_digest=release, session_id="session.alpha")
    second = store.create_session(active_release_digest=release, session_id="session.beta")

    assert first.runtime_kind == HARNESS_RUNTIME_KIND
    assert first.active_release_digest == release
    assert (project / HARNESS_SESSIONS_DIR_NAME / "session.alpha" / "session.json").exists()
    assert not (project / "releases" / release / HARNESS_SESSIONS_DIR_NAME).exists()
    assert first.session_id != second.session_id
    assert store.context_for_next("session.alpha", active_release_digest=release).carryover == ()
    assert store.context_for_next("session.beta", active_release_digest=release).carryover == ()

    message = store.append_message(
        "session.alpha",
        active_release_digest=release,
        expected_version=0,
        message_summary="Completed the public repair workflow with one retained artifact.",
        carryover=(
            _entry("artifacts/public-summary.json", digest_label="summary"),
            _entry("evidence/public-proof.json", digest_label="proof", summary="Public evidence digest and short result note."),
        ),
    )

    alpha_context = store.context_for_next("session.alpha", active_release_digest=release)
    beta_context = store.context_for_next("session.beta", active_release_digest=release)

    assert alpha_context.parent_message_id == message.message_id
    assert alpha_context.next_sequence == 1
    assert [entry.artifact_ref for entry in alpha_context.carryover] == [
        "artifacts/public-summary.json",
        "evidence/public-proof.json",
    ]
    assert all(entry.public_safe is True for entry in alpha_context.carryover)
    assert beta_context.parent_message_id is None
    assert beta_context.carryover == ()


def test_old_session_is_rejected_after_active_release_changes_without_migration(tmp_path: Path) -> None:
    release = _digest("release-one")
    changed_release = _digest("release-two")
    store = HarnessSessionStore(tmp_path)
    manifest = store.create_session(active_release_digest=release, session_id="session.release-pinned")

    with pytest.raises(HarnessSessionReleaseMismatchError, match="pinned to immutable release"):
        store.load_for_continuation(
            "session.release-pinned",
            active_release_digest=changed_release,
        )
    with pytest.raises(HarnessSessionReleaseMismatchError, match="start a new session"):
        store.context_for_next(
            "session.release-pinned",
            active_release_digest=changed_release,
        )

    unchanged = store.recover("session.release-pinned")
    assert unchanged.active_release_digest == manifest.active_release_digest
    assert unchanged.version == 0


def test_single_writer_lock_and_optimistic_sequence_checks(tmp_path: Path) -> None:
    release = _digest("release")
    store = HarnessSessionStore(tmp_path)
    store.create_session(active_release_digest=release, session_id="session.locked")

    lock_path = tmp_path / HARNESS_SESSIONS_DIR_NAME / "session.locked" / ".writer.lock"
    lock_path.write_text("external-writer", encoding="utf-8")
    with pytest.raises(HarnessSessionConcurrencyError):
        store.append_message(
            "session.locked",
            active_release_digest=release,
            expected_version=0,
            message_summary="This writer should be rejected while the lock exists.",
            carryover=(),
        )
    lock_path.unlink()

    store.append_message(
        "session.locked",
        active_release_digest=release,
        expected_version=0,
        message_summary="First public message summary.",
        carryover=(_entry(),),
    )
    with pytest.raises(HarnessSessionVersionError, match="current version is 1"):
        store.append_message(
            "session.locked",
            active_release_digest=release,
            expected_version=0,
            message_summary="Stale writer tries to append with old version.",
            carryover=(),
        )
    second = store.append_message(
        "session.locked",
        active_release_digest=release,
        expected_version=1,
        message_summary="Second public message summary.",
        carryover=(),
    )
    context = store.context_for_next("session.locked", active_release_digest=release)
    assert second.sequence == 1
    assert context.next_sequence == 2
    assert context.carryover == ()


def test_recover_aborts_uncommitted_prepare_and_finishes_committed_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = _digest("release")
    store = HarnessSessionStore(tmp_path)
    store.create_session(active_release_digest=release, session_id="session.recover")

    def fail_message_write(_message):
        raise RuntimeError("boom after prepare")

    monkeypatch.setattr(store, "_write_message_file", fail_message_write)
    with pytest.raises(RuntimeError, match="boom after prepare"):
        store.append_message(
            "session.recover",
            active_release_digest=release,
            expected_version=0,
            message_summary="Prepared but not committed.",
            carryover=(_entry(),),
        )
    monkeypatch.undo()

    recovered = store.recover("session.recover", active_release_digest=release)
    assert recovered.version == 0
    assert recovered.carryover == ()
    assert not list((tmp_path / HARNESS_SESSIONS_DIR_NAME / "session.recover" / "messages").glob("*.json"))

    original_write_manifest = store._write_manifest

    def fail_manifest_write(_manifest):
        raise RuntimeError("boom after commit")

    monkeypatch.setattr(store, "_write_manifest", fail_manifest_write)
    with pytest.raises(RuntimeError, match="boom after commit"):
        store.append_message(
            "session.recover",
            active_release_digest=release,
            expected_version=0,
            message_summary="Committed before manifest replacement.",
            carryover=(_entry(summary="Public carryover after committed recovery."),),
        )
    monkeypatch.setattr(store, "_write_manifest", original_write_manifest)

    recovered = store.recover("session.recover", active_release_digest=release)
    context = store.context_for_next("session.recover", active_release_digest=release)
    assert recovered.version == 1
    assert recovered.last_message_id is not None
    assert context.parent_message_id == recovered.last_message_id
    assert [entry.summary for entry in context.carryover] == [
        "Public carryover after committed recovery."
    ]
    assert len(list((tmp_path / HARNESS_SESSIONS_DIR_NAME / "session.recover" / "messages").glob("*.json"))) == 1


def test_public_carryover_rejects_traversal_duplicates_oversize_and_secrets(tmp_path: Path) -> None:
    release = _digest("release")
    store = HarnessSessionStore(
        tmp_path,
        limits=HarnessSessionLimits(max_entries=2, max_total_bytes=280, max_summary_bytes=40),
    )
    store.create_session(active_release_digest=release, session_id="session.public-safe")

    bad_cases = [
        (("bad.traversal", (_entry("../hidden.json"),)), "traverse"),
        (("bad.duplicate", (_entry("artifacts/a.json"), _entry("artifacts/a.json", digest_label="b"))), "duplicate"),
        (("bad.entry-limit", (_entry("artifacts/a.json"), _entry("artifacts/b.json"), _entry("artifacts/c.json"))), "entry limit"),
        (("bad.summary-limit", (_entry(summary="x" * 41),)), "summary byte limit"),
        (("bad.secret", (_entry(summary="public note with api_key inside"),)), "non-public"),
        (("bad.key", (_entry(summary="Bearer abcdefghijklmnopqrstuvwxyz012345"),)), "credential"),
        (("bad.snapshot", (_entry(ref="artifacts/repository_snapshot.json"),)), "non-public"),
        (("bad.patch", (_entry(summary="raw patch diff --git a/x b/x"),)), "non-public"),
    ]
    for (message_summary, carryover), match in bad_cases:
        with pytest.raises((HarnessSessionValidationError, ValueError), match=match):
            store.append_message(
                "session.public-safe",
                active_release_digest=release,
                expected_version=0,
                message_summary=message_summary,
                carryover=carryover,
            )

    with pytest.raises(HarnessSessionValidationError, match="total byte limit"):
        store.append_message(
            "session.public-safe",
            active_release_digest=release,
            expected_version=0,
            message_summary="bad.total",
            carryover=(
                _entry("artifacts/a.json", summary="x" * 35),
                _entry("artifacts/b.json", digest_label="b", summary="y" * 35),
            ),
        )

    store.append_message(
        "session.public-safe",
        active_release_digest=release,
        expected_version=0,
        message_summary="good",
        carryover=(_entry(summary="short public summary"),),
    )
    context_payload = json.loads(
        (tmp_path / HARNESS_SESSIONS_DIR_NAME / "session.public-safe" / "session.json").read_text(
            encoding="utf-8"
        )
    )
    assert "predictor" not in json.dumps(context_payload).casefold()
    assert "repository_snapshot" not in json.dumps(context_payload).casefold()
