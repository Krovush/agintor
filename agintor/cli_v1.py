from __future__ import annotations

import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Literal, Optional

import typer

from .core.redaction import redact_sensitive_text


CLI_ERROR_SCHEMA_VERSION = "repo-repair-harness-cli-error-v1"
CLI_BUILD_RESULT_SCHEMA_VERSION = "repo-repair-harness-cli-build-result-v1"
CLI_EVAL_RESULT_SCHEMA_VERSION = "repo-repair-harness-cli-eval-result-v1"
CLI_GATE0_DRY_RUN_RESULT_SCHEMA_VERSION = (
    "repo-repair-harness-cli-gate0-dry-run-result-v1"
)
CLI_INSPECT_RESULT_SCHEMA_VERSION = "repo-repair-harness-cli-inspect-result-v1"
CLI_PILOT_DRY_RUN_RESULT_SCHEMA_VERSION = (
    "repo-repair-harness-cli-pilot-dry-run-result-v1"
)
CLI_READINESS_RESULT_SCHEMA_VERSION = (
    "repo-repair-harness-cli-readiness-result-v1"
)
CLI_SEARCH_DRY_RUN_RESULT_SCHEMA_VERSION = (
    "repo-repair-harness-cli-search-dry-run-result-v1"
)
CLI_SOLVE_RESULT_SCHEMA_VERSION = "repo-repair-harness-cli-solve-result-v1"

_PROCESS_ROLE_ENV = "AGINTOR_PROCESS_ROLE"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]*$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
_MAX_JSON_BYTES = 64 * 1024 * 1024
_MAX_API_KEY_FILE_BYTES = 16 * 1024
_BASE_CHILD_ENV = frozenset(
    {
        "COMSPEC",
        "HOME",
        "LANG",
        "LC_ALL",
        "NO_PROXY",
        "NUMBER_OF_PROCESSORS",
        "PATH",
        "PATHEXT",
        "PROCESSOR_ARCHITECTURE",
        "REQUESTS_CA_BUNDLE",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
        "TMPDIR",
        "WINDIR",
    }
)


app = typer.Typer(
    add_completion=False,
    help="Build and run the bounded Agintor repo-repair harness V1.",
    no_args_is_help=True,
)


class CliV1Error(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        self.public_message = message
        super().__init__(message)


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(
            dict(payload),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _echo_json(payload: Mapping[str, Any]) -> None:
    typer.echo(_json_bytes(payload).decode("utf-8"), nl=False)


def _write_json_atomic(path: str | Path, payload: Mapping[str, Any]) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_symlink():
        raise CliV1Error(
            "output_path_invalid",
            "structured output path may not be a symlink",
        )
    destination = candidate.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(
        f".{destination.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with temporary.open("xb") as handle:
            handle.write(_json_bytes(payload))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)
    return destination


def _safe_external_output_path(
    *,
    project_root: Path,
    request_path: str | Path,
    output_path: str | Path,
) -> Path:
    raw_request = Path(request_path).expanduser()
    raw_destination = Path(output_path).expanduser()
    if raw_destination.is_symlink():
        raise CliV1Error(
            "output_path_invalid",
            "public evaluator output may not be a symlink",
        )
    request = raw_request.resolve()
    destination = raw_destination.resolve()
    if destination == request:
        raise CliV1Error(
            "output_path_invalid",
            "public output may not replace its evaluator request",
        )
    if destination == project_root or project_root in destination.parents:
        raise CliV1Error(
            "output_path_invalid",
            "public evaluator output must remain outside the harness factory project",
        )
    if destination.exists() and destination.is_dir():
        raise CliV1Error(
            "output_path_invalid",
            "public evaluator output must be a regular file destination",
        )
    return destination


def _error_payload(command: str, exc: Exception) -> dict[str, Any]:
    if isinstance(exc, CliV1Error):
        code = exc.code
        message = redact_sensitive_text(exc.public_message)
    else:
        known_errors = {
            "HarnessSessionReleaseMismatchError": "session_release_mismatch",
            "HarnessSessionConcurrencyError": "session_concurrency_conflict",
            "HarnessSessionVersionError": "session_version_conflict",
            "HarnessSessionValidationError": "session_validation_failed",
            "HarnessFactoryStaleHeadError": "factory_stale_head",
            "HarnessFactoryConcurrencyError": "factory_concurrency_conflict",
        }
        error_name = type(exc).__name__
        code = known_errors.get(error_name, "operation_failed")
        message = redact_sensitive_text(
            str(exc)
            if error_name in known_errors
            else f"{error_name}: {command} failed"
        )
    return {
        "schema_version": CLI_ERROR_SCHEMA_VERSION,
        "status": "failed",
        "operation": command,
        "code": code,
        "message": message,
    }


def _fail(command: str, exc: Exception) -> None:
    _echo_json(_error_payload(command, exc))
    raise typer.Exit(code=2)


def _read_json_object(path: str | Path, *, code: str, label: str) -> dict[str, Any]:
    candidate = Path(path).expanduser()
    try:
        if candidate.is_symlink() or not candidate.is_file():
            raise FileNotFoundError
        source = candidate.resolve()
        raw = source.read_bytes()
    except OSError as exc:
        raise CliV1Error(code, f"{label} is missing or is not a regular file") from exc
    if len(raw) > _MAX_JSON_BYTES:
        raise CliV1Error(code, f"{label} exceeds the maximum JSON size")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key {key!r}")
            result[key] = value
        return result

    try:
        payload = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise CliV1Error(code, f"{label} must be valid UTF-8 JSON") from exc
    if not isinstance(payload, Mapping):
        raise CliV1Error(code, f"{label} JSON root must be an object")
    return dict(payload)


def _regular_input_path(
    path: str | Path,
    *,
    code: str,
    label: str,
) -> Path:
    candidate = Path(path).expanduser()
    if candidate.is_symlink() or not candidate.is_file():
        raise CliV1Error(code, f"{label} is missing or is not a regular file")
    return candidate.resolve()


def _exact_request_keys(
    payload: Mapping[str, Any],
    *,
    expected: set[str],
    code: str,
    label: str,
) -> None:
    actual = set(payload)
    if actual != expected:
        raise CliV1Error(
            code,
            f"{label} has an unexpected schema",
        )


def _controlled_evidence_path(
    project_root: Path,
    relative_path: str,
) -> Path:
    normalized = str(relative_path or "").strip().replace("\\", "/")
    relative = PurePosixPath(normalized)
    if (
        not normalized
        or relative.is_absolute()
        or ".." in relative.parts
        or not relative.parts
        or relative.parts[0] != "controlled_development_and_evaluator_evidence"
    ):
        raise CliV1Error(
            "evidence_path_invalid",
            "dry-run evidence must use a controlled project-relative path",
        )
    candidate = project_root.joinpath(*relative.parts)
    for path in (candidate, *candidate.parents):
        if path == project_root.parent:
            break
        if path.exists() and path.is_symlink():
            raise CliV1Error(
                "evidence_path_invalid",
                "dry-run evidence path may not cross a symlink",
            )
    resolved = candidate.resolve()
    if project_root not in resolved.parents:
        raise CliV1Error(
            "evidence_path_invalid",
            "dry-run evidence path escapes the factory project",
        )
    return resolved


def _write_json_immutable(path: Path, payload: Mapping[str, Any]) -> Path:
    raw = _json_bytes(payload)
    if path.exists():
        if path.is_symlink() or not path.is_file():
            raise CliV1Error(
                "evidence_path_invalid",
                "immutable dry-run evidence path is not a regular file",
            )
        if path.read_bytes() != raw:
            raise CliV1Error(
                "evidence_conflict",
                "refusing to overwrite different immutable dry-run evidence",
            )
        return path
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(raw)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def _canonical_project_root(value: str | Path, *, create: bool) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_symlink():
        raise CliV1Error(
            "project_root_invalid",
            "factory project root must be a real directory, not a symlink",
        )
    root = candidate.resolve()
    if root.exists() and not root.is_dir():
        raise CliV1Error(
            "project_root_invalid",
            "factory project root must be a real directory, not a symlink",
        )
    if create:
        root.mkdir(parents=True, exist_ok=True)
    elif not root.is_dir():
        raise CliV1Error(
            "project_root_missing",
            "factory project root does not exist",
        )
    return root


def _canonical_project_destination(value: str | Path) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_symlink():
        raise CliV1Error(
            "project_root_invalid",
            "factory project root must be a real directory, not a symlink",
        )
    root = candidate.resolve()
    if root.exists() and not root.is_dir():
        raise CliV1Error(
            "project_root_invalid",
            "factory project root must be a real directory, not a symlink",
        )
    return root


def _canonical_controlled_root(value: str | Path) -> Path:
    candidate = Path(value).expanduser()
    if candidate.is_symlink():
        raise CliV1Error(
            "controlled_root_invalid",
            "readiness controlled root may not be a symlink",
        )
    root = candidate.resolve()
    if not root.is_dir():
        raise CliV1Error(
            "controlled_root_invalid",
            "readiness controlled root must be an existing directory",
        )
    return root


def _controlled_request_file(root: Path, value: str | Path) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    if candidate.is_symlink():
        raise CliV1Error(
            "readiness_request_path_invalid",
            "readiness request may not be a symlink",
        )
    request = candidate.resolve()
    if root not in request.parents or not request.is_file():
        raise CliV1Error(
            "readiness_request_path_invalid",
            "readiness request must be a regular file inside the controlled root",
        )
    current = candidate
    while current != root:
        if current.exists() and current.is_symlink():
            raise CliV1Error(
                "readiness_request_path_invalid",
                "readiness request may not cross a symlink",
            )
        if current == current.parent:
            raise CliV1Error(
                "readiness_request_path_invalid",
                "readiness request escaped the controlled root",
            )
        current = current.parent
    return request


@contextmanager
def _process_role(role: str) -> Iterator[None]:
    existing = os.environ.get(_PROCESS_ROLE_ENV)
    normalized = str(existing or "").strip().casefold()
    if normalized and normalized != role:
        raise CliV1Error(
            "process_role_conflict",
            f"{role} operation cannot run in a {normalized!r} process role",
        )
    if not normalized:
        os.environ[_PROCESS_ROLE_ENV] = role
    try:
        yield
    finally:
        if not normalized:
            os.environ.pop(_PROCESS_ROLE_ENV, None)


def _require_digest(value: Any, label: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _DIGEST_RE.fullmatch(normalized):
        raise CliV1Error("active_release_invalid", f"active release {label} is invalid")
    return normalized


def _assert_evaluator_authority_absent(process_role: str) -> None:
    forbidden = (
        "agintor.evaluation.contracts",
        "agintor.oracle.package_io",
    )
    loaded = [name for name in forbidden if name in sys.modules]
    if loaded:
        raise CliV1Error(
            "authority_firewall_violation",
            f"{process_role} process loaded evaluator-only authority code",
        )


def _assert_unprivileged_launcher() -> None:
    existing = str(os.environ.get(_PROCESS_ROLE_ENV) or "").strip()
    if existing:
        raise CliV1Error(
            "process_role_conflict",
            "evaluator launcher must start unprivileged and grant authority only to its child process",
        )


def _active_release(project_root: Path) -> tuple[dict[str, Any], Path]:
    pointer_path = project_root / "active_release.json"
    pointer = _read_json_object(
        pointer_path,
        code="active_release_missing",
        label="active release pointer",
    )
    if set(pointer) != {
        "runtime_kind",
        "release_digest",
        "release_path",
        "manifest_digest",
    }:
        raise CliV1Error(
            "active_release_invalid",
            "active release pointer has an unexpected schema",
        )
    if pointer.get("runtime_kind") != "harness":
        raise CliV1Error(
            "runtime_kind_unsupported",
            "the active release must have runtime_kind='harness'",
        )
    release_digest = _require_digest(pointer.get("release_digest"), "digest")
    _require_digest(pointer.get("manifest_digest"), "manifest digest")
    raw_path = str(pointer.get("release_path") or "").strip().replace("\\", "/")
    release_path = PurePosixPath(raw_path)
    if (
        release_path.is_absolute()
        or ".." in release_path.parts
        or release_path.parts != ("releases", release_digest)
    ):
        raise CliV1Error(
            "active_release_invalid",
            "active release path is not the content-addressed harness generation",
        )
    generation_candidate = project_root.joinpath(*release_path.parts)
    if generation_candidate.is_symlink():
        raise CliV1Error(
            "active_release_invalid",
            "active release generation is missing or unsafe",
        )
    generation = generation_candidate.resolve()
    releases_root = (project_root / "releases").resolve()
    if generation.parent != releases_root or not generation.is_dir():
        raise CliV1Error(
            "active_release_invalid",
            "active release generation is missing or unsafe",
        )
    return pointer, generation


def _profile_environment(
    generation: Path,
) -> tuple[set[str], Optional[str], Optional[str]]:
    profile_path = generation / "runtime" / "runtime_profile.json"
    projection = _read_json_object(
        profile_path,
        code="active_release_invalid",
        label="frozen harness deployment profile",
    )
    if projection.get("runtime_kind") != "harness":
        raise CliV1Error(
            "runtime_kind_unsupported",
            "the frozen deployment profile must have runtime_kind='harness'",
        )
    profile = projection.get("profile")
    endpoint = profile.get("endpoint") if isinstance(profile, Mapping) else None
    if not isinstance(endpoint, Mapping):
        raise CliV1Error(
            "active_release_invalid",
            "the frozen harness provider endpoint is missing",
        )
    names: dict[str, str] = {}
    for field in ("api_key_env", "api_key_file_env", "base_url_env"):
        value = endpoint.get(field)
        if value is None:
            continue
        name = str(value).strip()
        if not _ENV_NAME_RE.fullmatch(name):
            raise CliV1Error(
                "active_release_invalid",
                "the frozen provider endpoint contains an invalid environment name",
            )
        names[field] = name
    return (
        {names["base_url_env"]} if "base_url_env" in names else set(),
        names.get("api_key_env"),
        names.get("api_key_file_env"),
    )


def _child_environment(
    generation: Path,
    *,
    role: str,
    api_key_file: Optional[str] = None,
    allow_credentials: bool = False,
    forbidden_credential_roots: Sequence[Path] = (),
) -> dict[str, str]:
    configured_names, api_key_env, api_key_file_env = _profile_environment(generation)
    if allow_credentials:
        configured_names.update(
            name for name in (api_key_env, api_key_file_env) if name is not None
        )
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _BASE_CHILD_ENV or key in configured_names
    }
    if api_key_file is not None:
        if not allow_credentials:
            raise CliV1Error(
                "key_file_reference_forbidden",
                "credential references are forbidden outside live solve execution",
            )
        if api_key_file_env is None:
            raise CliV1Error(
                "key_file_reference_forbidden",
                "the frozen deployment profile does not declare an API key-file environment reference",
            )
        credential_candidate = Path(api_key_file).expanduser()
        try:
            lexical_path = credential_candidate.absolute()
            credential_stat = credential_candidate.lstat()
            credential_path = credential_candidate.resolve(strict=True)
        except OSError as exc:
            raise CliV1Error(
                "key_file_reference_invalid",
                "the API key-file reference must name an existing regular file",
            ) from exc
        reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))
        file_attributes = int(getattr(credential_stat, "st_file_attributes", 0))
        if (
            credential_candidate.is_symlink()
            or bool(reparse_flag and file_attributes & reparse_flag)
            or not stat.S_ISREG(credential_stat.st_mode)
            or credential_stat.st_size <= 0
            or credential_stat.st_size > _MAX_API_KEY_FILE_BYTES
        ):
            raise CliV1Error(
                "key_file_reference_invalid",
                "the API key-file reference must name a bounded regular non-link file",
            )
        roots = (generation, *forbidden_credential_roots)
        for root in roots:
            resolved_root = Path(root).expanduser().resolve(strict=False)
            if (
                credential_path == resolved_root
                or resolved_root in credential_path.parents
                or lexical_path == resolved_root
                or resolved_root in lexical_path.parents
            ):
                raise CliV1Error(
                    "key_file_reference_forbidden",
                    "the API key file must remain outside project, release, and run workspaces",
                )
        if api_key_env is not None:
            environment.pop(api_key_env, None)
        environment[api_key_file_env] = str(credential_path)
    environment[_PROCESS_ROLE_ENV] = role
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONUTF8"] = "1"
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    return environment


def _run_source_hidden_entry(
    *,
    project_root: Path,
    command: str,
    request_payload: Optional[Mapping[str, Any]] = None,
    timeout_seconds: float = 3600.0,
    api_key_file: Optional[str] = None,
    allow_credentials: bool = False,
) -> dict[str, Any]:
    _pointer, generation = _active_release(project_root)
    bundle_candidate = generation / "runtime" / "runtime_sdk"
    if bundle_candidate.is_symlink():
        raise CliV1Error(
            "runtime_bundle_missing",
            "active harness release has no safe source-hidden runtime bundle",
        )
    bundle_root = bundle_candidate.resolve()
    if (
        not bundle_root.is_dir()
        or bundle_root.parent != (generation / "runtime").resolve()
    ):
        raise CliV1Error(
            "runtime_bundle_missing",
            "active harness release has no safe source-hidden runtime bundle",
        )
    forbidden_credential_roots = [project_root]
    if request_payload is not None:
        for field_name in ("run_artifact_workspace", "run_root"):
            value = request_payload.get(field_name)
            if isinstance(value, str) and value.strip():
                candidate = Path(value).expanduser()
                forbidden_credential_roots.append(
                    candidate if candidate.is_absolute() else project_root / candidate
                )
        snapshot_source = request_payload.get("workspace_snapshot_source_path")
        if isinstance(snapshot_source, str) and snapshot_source.strip():
            forbidden_credential_roots.append(Path(snapshot_source).expanduser())
    environment = _child_environment(
        generation,
        role="runtime",
        api_key_file=api_key_file,
        allow_credentials=allow_credentials,
        forbidden_credential_roots=forbidden_credential_roots,
    )
    entry_script = (
        "import os,sys; "
        "assert os.environ.get('AGINTOR_PROCESS_ROLE') == 'runtime'; "
        f"sys.path.insert(0, {str(bundle_root)!r}); "
        "from agintor_runtime.runtime_entry import main; "
        "assert 'agintor_runtime.evaluation.contracts' not in sys.modules; "
        "assert 'agintor.evaluation.contracts' not in sys.modules; "
        "raise SystemExit(main(sys.argv[1:]))"
    )
    with tempfile.TemporaryDirectory(prefix="agintor-harness-entry-") as temporary:
        temporary_root = Path(temporary)
        output_path = temporary_root / "output.json"
        arguments = [
            sys.executable,
            "-I",
            "-B",
            "-c",
            entry_script,
            command,
            "--project-root",
            str(project_root),
            "--output-json",
            str(output_path),
        ]
        if request_payload is not None:
            request_path = temporary_root / "request.json"
            request_path.write_bytes(_json_bytes(request_payload))
            arguments.extend(["--request-json", str(request_path)])
        try:
            completed = subprocess.run(
                arguments,
                cwd=str(temporary_root),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=False,
                shell=False,
                timeout=max(1.0, timeout_seconds),
                check=False,
                creationflags=(
                    int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    if os.name == "nt"
                    else 0
                ),
            )
        except subprocess.TimeoutExpired as exc:
            raise CliV1Error(
                "runtime_subprocess_timeout",
                "source-hidden harness runtime exceeded its frozen execution deadline",
            ) from exc
        if not output_path.is_file() or output_path.is_symlink():
            raise CliV1Error(
                "runtime_subprocess_failed",
                "source-hidden harness runtime did not produce a structured result",
            )
        payload = _read_json_object(
            output_path,
            code="runtime_result_invalid",
            label="source-hidden harness runtime result",
        )
        if completed.returncode != 0 or payload.get("status") == "failed":
            code = str(payload.get("code") or "runtime_subprocess_failed")
            message = str(
                payload.get("message")
                or "source-hidden harness runtime rejected the operation"
            )
            if not _IDENTIFIER_RE.fullmatch(code):
                code = "runtime_subprocess_failed"
            raise CliV1Error(code, message)
        return payload


def _run_evaluator_entry(
    *,
    project_root: Path,
    request_path: str | Path,
    public_output_path: str | Path,
    timeout_seconds: float = 3600.0,
) -> dict[str, Any]:
    request_candidate = Path(request_path).expanduser()
    try:
        if request_candidate.is_symlink() or not request_candidate.is_file():
            raise FileNotFoundError
        request_file = request_candidate.resolve()
        if request_file.stat().st_size > _MAX_JSON_BYTES:
            raise CliV1Error(
                "evaluation_request_invalid",
                "evaluator request exceeds the maximum JSON size",
            )
    except OSError as exc:
        raise CliV1Error(
            "evaluation_request_invalid",
            "evaluator request is missing or is not a regular file",
        ) from exc
    allowed_environment = set(_BASE_CHILD_ENV)
    allowed_environment.update(
        {
            "DOCKER_CERT_PATH",
            "DOCKER_CONTEXT",
            "DOCKER_HOST",
            "DOCKER_TLS_VERIFY",
        }
    )
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in allowed_environment
    }
    environment[_PROCESS_ROLE_ENV] = "evaluator"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONUTF8"] = "1"
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    with tempfile.TemporaryDirectory(prefix="agintor-evaluator-entry-") as temporary:
        output_path = Path(temporary) / "output.json"
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "agintor.evaluation.harness_entrypoint",
                    "--project-root",
                    str(project_root),
                    "--request-json",
                    str(request_file),
                    "--output-json",
                    str(output_path),
                    "--public-output-path",
                    str(Path(public_output_path).expanduser().resolve()),
                ],
                cwd=str(Path(__file__).resolve().parent.parent),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=False,
                shell=False,
                timeout=max(1.0, timeout_seconds),
                check=False,
                creationflags=(
                    int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    if os.name == "nt"
                    else 0
                ),
            )
        except subprocess.TimeoutExpired as exc:
            raise CliV1Error(
                "evaluator_subprocess_timeout",
                "isolated evaluator exceeded its execution deadline",
            ) from exc
        if not output_path.is_file() or output_path.is_symlink():
            raise CliV1Error(
                "evaluator_subprocess_failed",
                "isolated evaluator did not produce a structured result",
            )
        payload = _read_json_object(
            output_path,
            code="evaluation_result_invalid",
            label="isolated evaluator result",
        )
        if completed.returncode != 0 or payload.get("status") == "failed":
            code = str(payload.get("code") or "evaluator_subprocess_failed")
            message = str(
                payload.get("message")
                or "isolated evaluator rejected the operation"
            )
            if not _IDENTIFIER_RE.fullmatch(code):
                code = "evaluator_subprocess_failed"
            raise CliV1Error(code, message)
        return payload


def _run_readiness_entry(
    *,
    controlled_root: Path,
    request_path: Path,
    timeout_seconds: float = 3600.0,
) -> dict[str, Any]:
    _assert_unprivileged_launcher()
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in _BASE_CHILD_ENV
    }
    environment[_PROCESS_ROLE_ENV] = "evaluator"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONUTF8"] = "1"
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    with tempfile.TemporaryDirectory(prefix="agintor-readiness-entry-") as temporary:
        output_path = Path(temporary) / "output.json"
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    "-B",
                    "-m",
                    "agintor.evaluation.readiness_entrypoint",
                    "--controlled-root",
                    str(controlled_root),
                    "--request-json",
                    str(request_path),
                    "--output-json",
                    str(output_path),
                ],
                cwd=str(Path(__file__).resolve().parent.parent),
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=False,
                shell=False,
                timeout=max(1.0, timeout_seconds),
                check=False,
                creationflags=(
                    int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
                    if os.name == "nt"
                    else 0
                ),
            )
        except subprocess.TimeoutExpired as exc:
            raise CliV1Error(
                "readiness_subprocess_timeout",
                "controlled readiness child exceeded its offline validation deadline",
            ) from exc
        if not output_path.is_file() or output_path.is_symlink():
            raise CliV1Error(
                "readiness_subprocess_failed",
                "controlled readiness child did not produce a structured result",
            )
        payload = _read_json_object(
            output_path,
            code="readiness_result_invalid",
            label="controlled readiness child result",
        )
        if completed.returncode != 0 or payload.get("status") == "failed":
            code = str(payload.get("code") or "readiness_subprocess_failed")
            message = str(
                payload.get("message")
                or "controlled readiness child rejected the operation"
            )
            if not _IDENTIFIER_RE.fullmatch(code):
                code = "readiness_subprocess_failed"
            raise CliV1Error(code, message)
        return payload


def _public_readiness_result(
    payload: Mapping[str, Any],
    *,
    expected_operation: Literal["build", "replay"],
) -> dict[str, Any]:
    expected_keys = {
        "schema_version",
        "status",
        "operation",
        "live_status",
        "inference_requests_sent",
        "packet_id",
        "packet_digest",
        "packet_path",
    }
    if set(payload) != expected_keys or payload.get("schema_version") != (
        "repo-repair-harness-readiness-entry-result-v1"
    ):
        raise CliV1Error(
            "readiness_result_invalid",
            "controlled readiness child returned an unexpected result schema",
        )
    if (
        payload.get("status") != "succeeded"
        or payload.get("operation") != expected_operation
        or payload.get("live_status") != "not_run"
        or payload.get("inference_requests_sent") != 0
    ):
        raise CliV1Error(
            "readiness_result_invalid",
            "controlled readiness child crossed its offline operation boundary",
        )
    packet_id = str(payload.get("packet_id") or "").strip()
    packet_digest = str(payload.get("packet_digest") or "").strip().lower()
    raw_packet_path = str(payload.get("packet_path") or "").strip().replace(
        "\\", "/"
    )
    packet_path = PurePosixPath(raw_packet_path)
    if (
        not _IDENTIFIER_RE.fullmatch(packet_id)
        or not _DIGEST_RE.fullmatch(packet_digest)
        or not raw_packet_path
        or packet_path.is_absolute()
        or ".." in packet_path.parts
        or re.fullmatch(r"[A-Za-z]:", packet_path.parts[0] if packet_path.parts else "")
    ):
        raise CliV1Error(
            "readiness_result_invalid",
            "controlled readiness child returned an unsafe public identity",
        )
    return {
        "schema_version": CLI_READINESS_RESULT_SCHEMA_VERSION,
        "status": "succeeded",
        "operation": f"readiness-{expected_operation}",
        "live_status": "not_run",
        "real_inference_requests_sent": 0,
        "packet_id": packet_id,
        "packet_digest": packet_digest,
        "packet_path": packet_path.as_posix(),
    }


def _load_build_input(project_root: Path, request_json: str | Path):
    payload = _read_json_object(
        request_json,
        code="build_request_invalid",
        label="harness factory build request",
    )
    request_project = payload.get("project_root")
    if request_project is None or Path(str(request_project)).expanduser().resolve() != project_root:
        raise CliV1Error(
            "build_project_mismatch",
            "build request project_root must exactly match the CLI factory project",
        )
    try:
        from .factory.harness_service import HarnessFactoryBuildInput

        build_input = HarnessFactoryBuildInput.model_validate(payload)
    except CliV1Error:
        raise
    except Exception as exc:
        raise CliV1Error(
            "build_request_invalid",
            "harness factory build request failed strict typed validation",
        ) from exc
    if build_input.project_root and Path(build_input.project_root).expanduser().resolve() != project_root:
        raise CliV1Error(
            "build_project_mismatch",
            "validated build request crossed the CLI factory project",
        )
    return build_input


@app.command("build-runtime")
def build_runtime_command(
    project_root: str = typer.Argument(
        ...,
        help="Harness factory project directory.",
    ),
    request_json: str = typer.Option(
        ...,
        "--request-json",
        help="Strict typed HarnessFactoryBuildInput JSON.",
    ),
    replay_manifest: Optional[str] = typer.Option(
        None,
        "--replay-manifest",
        help="Immutable deterministic factory replay for offline_scripted builds.",
    ),
) -> None:
    """Validate authority inputs, dry-run, or publish a replayed harness release."""

    try:
        root = _canonical_project_destination(project_root)
        with _process_role("factory"):
            _assert_evaluator_authority_absent("factory")
            build_input = _load_build_input(root, request_json)
            if build_input.execution_mode == "dry_run":
                if replay_manifest is not None:
                    raise CliV1Error(
                        "dry_run_replay_forbidden",
                        "dry-run factory builds cannot receive replay callbacks",
                    )
                from .factory.harness_service import build_harness_factory_release

                service_result = build_harness_factory_release(build_input)
                replay_provenance = None
                replay_provenance_path = None
            elif build_input.execution_mode == "offline_scripted":
                if replay_manifest is None:
                    raise CliV1Error(
                        "factory_replay_required",
                        "offline_scripted factory builds require an immutable replay manifest",
                    )
                replay_path = _regular_input_path(
                    replay_manifest,
                    code="factory_replay_invalid",
                    label="factory replay manifest",
                )
                root.mkdir(parents=True, exist_ok=True)
                from .factory.harness_replay import (
                    build_harness_factory_release_from_replay,
                )

                replayed = build_harness_factory_release_from_replay(
                    build_input,
                    replay_manifest_path=replay_path,
                )
                service_result = replayed.service_result
                replay_provenance = replayed.provenance.model_dump(mode="json")
                replay_provenance_path = replayed.provenance_path
            else:
                raise CliV1Error(
                    "factory_mode_unsupported",
                    "harness V1 factory supports only dry_run and offline_scripted replay",
                )
            _assert_evaluator_authority_absent("factory")
        _echo_json(
            {
                "schema_version": CLI_BUILD_RESULT_SCHEMA_VERSION,
                "status": "succeeded",
                "operation": "build-runtime",
                "runtime_kind": "harness",
                "execution_mode": service_result.execution_mode,
                "project_root": str(root),
                "result": service_result.model_dump(mode="json", exclude_none=True),
                "replay_provenance": replay_provenance,
                "replay_provenance_path": replay_provenance_path,
            }
        )
    except (typer.Exit, KeyboardInterrupt):
        raise
    except Exception as exc:
        _fail("build-runtime", exc)


@app.command("gate0-dry-run")
def gate0_dry_run_command(
    project_root: str = typer.Argument(
        ...,
        help="Harness project receiving controlled Gate 0 evidence.",
    ),
    request_json: str = typer.Option(
        ...,
        "--request-json",
        help="Strict Gate 0 deployment profile and evidence-path request JSON.",
    ),
) -> None:
    """Freeze and persist the exact Gate 0 schedule without provider calls."""

    try:
        request = _read_json_object(
            request_json,
            code="gate0_request_invalid",
            label="Gate 0 dry-run request",
        )
        _exact_request_keys(
            request,
            expected={
                "schema_version",
                "deployment_profile",
                "provider_evidence_destination",
                "manifest_destination",
            },
            code="gate0_request_invalid",
            label="Gate 0 dry-run request",
        )
        if request["schema_version"] != "repo-repair-harness-cli-gate0-dry-run-request-v1":
            raise CliV1Error(
                "gate0_request_invalid",
                "unsupported Gate 0 dry-run request schema",
            )
        root = _canonical_project_destination(project_root)
        provider_destination = _controlled_evidence_path(
            root,
            str(request["provider_evidence_destination"]),
        )
        manifest_destination = _controlled_evidence_path(
            root,
            str(request["manifest_destination"]),
        )
        if provider_destination == manifest_destination:
            raise CliV1Error(
                "gate0_request_invalid",
                "Gate 0 provider evidence and preregistration paths must differ",
            )
        with _process_role("factory"):
            _assert_evaluator_authority_absent("factory")
            from .contracts.run_evidence import assert_no_resolved_credentials
            from .evaluation.gate0 import (
                build_gate0_dry_run_manifest,
                build_gate0_provider_identity,
                validate_gate0_dry_run_conformance,
                write_gate0_preregistration,
            )
            from .runtime.harness_profile import HarnessDeploymentProfile

            assert_no_resolved_credentials(request)
            try:
                deployment_profile = HarnessDeploymentProfile.model_validate(
                    request["deployment_profile"]
                )
                provider_identity = build_gate0_provider_identity(
                    deployment_profile=deployment_profile,
                )
            except Exception as exc:
                raise CliV1Error(
                    "gate0_request_invalid",
                    "Gate 0 deployment profile failed strict typed validation",
                ) from exc
            relative_provider_destination = provider_destination.relative_to(root).as_posix()
            manifest = build_gate0_dry_run_manifest(
                provider_identity=provider_identity,
                evidence_destination=relative_provider_destination,
            )
            conformance = validate_gate0_dry_run_conformance(manifest)
            if not conformance.passed:
                raise CliV1Error(
                    "gate0_conformance_failed",
                    "Gate 0 deterministic dry-run conformance failed",
                )
            if manifest.live_status != "not_run" or any(
                call.request_sent
                for arm in manifest.arms
                for call in arm.calls
            ):
                raise CliV1Error(
                    "gate0_live_execution_forbidden",
                    "Gate 0 dry run attempted provider execution",
                )
            if manifest_destination.exists():
                persisted = _read_json_object(
                    manifest_destination,
                    code="gate0_evidence_conflict",
                    label="Gate 0 preregistration evidence",
                )
                if (
                    persisted.get("live_status") != "not_run"
                    or not isinstance(persisted.get("manifest"), Mapping)
                    or persisted["manifest"].get("manifest_digest")
                    != manifest.manifest_digest
                ):
                    raise CliV1Error(
                        "gate0_evidence_conflict",
                        "existing Gate 0 preregistration differs from this dry run",
                    )
            else:
                root.mkdir(parents=True, exist_ok=True)
                write_gate0_preregistration(manifest_destination, manifest)
            _assert_evaluator_authority_absent("factory")
        _echo_json(
            {
                "schema_version": CLI_GATE0_DRY_RUN_RESULT_SCHEMA_VERSION,
                "status": "succeeded",
                "operation": "gate0-dry-run",
                "runtime_kind": "harness",
                "live_status": "not_run",
                "real_inference_requests_sent": 0,
                "manifest_digest": manifest.manifest_digest,
                "planned_provider_calls": manifest.total_provider_calls,
                "provider_calls_sent": 0,
                "conformance_passed": conformance.passed,
                "evidence_path": str(manifest_destination),
            }
        )
    except (typer.Exit, KeyboardInterrupt):
        raise
    except Exception as exc:
        _fail("gate0-dry-run", exc)


@app.command("search-dry-run")
def search_dry_run_command(
    project_root: str = typer.Argument(
        ...,
        help="Harness factory project receiving controlled search evidence.",
    ),
    request_json: str = typer.Option(
        ...,
        "--request-json",
        help="Strict dry-run HarnessFactoryBuildInput JSON.",
    ),
) -> None:
    """Validate and persist the exact S1 development-search plan with no callbacks."""

    try:
        root = _canonical_project_destination(project_root)
        pointer_path = root / "active_release.json"
        prior_pointer = pointer_path.read_bytes() if pointer_path.is_file() else None
        with _process_role("factory"):
            _assert_evaluator_authority_absent("factory")
            build_input = _load_build_input(root, request_json)
            if build_input.execution_mode != "dry_run":
                raise CliV1Error(
                    "search_request_invalid",
                    "development search dry run requires execution_mode='dry_run'",
                )
            from .factory.harness_service import build_harness_factory_release

            result = build_harness_factory_release(build_input)
            manifest = result.dry_run_manifest
            if (
                manifest is None
                or manifest.live_status != "not_run"
                or any(manifest.callback_counts.values())
                or manifest.release_published
                or result.release_pointer is not None
            ):
                raise CliV1Error(
                    "search_live_execution_forbidden",
                    "development search dry run attempted callbacks or publication",
                )
            _assert_evaluator_authority_absent("factory")
        current_pointer = pointer_path.read_bytes() if pointer_path.is_file() else None
        if current_pointer != prior_pointer:
            raise CliV1Error(
                "search_pointer_changed",
                "development search dry run changed the active release pointer",
            )
        _echo_json(
            {
                "schema_version": CLI_SEARCH_DRY_RUN_RESULT_SCHEMA_VERSION,
                "status": "succeeded",
                "operation": "search-dry-run",
                "runtime_kind": "harness",
                "live_status": "not_run",
                "real_inference_requests_sent": 0,
                "search_result_digest": result.search_result_digest,
                "search_execution_status": result.search_execution_status,
                "proposal_callbacks_sent": manifest.callback_counts["proposal"],
                "evaluator_callbacks_sent": manifest.callback_counts["evaluator"],
                "release_published": manifest.release_published,
                "evidence_path": result.evidence_path,
                "manifest_digest": manifest.build_digest,
            }
        )
    except (typer.Exit, KeyboardInterrupt):
        raise
    except Exception as exc:
        _fail("search-dry-run", exc)


@app.command("pilot-dry-run")
def pilot_dry_run_command(
    project_root: str = typer.Argument(
        ...,
        help="Harness factory project with the exact active pilot release.",
    ),
    request_json: str = typer.Option(
        ...,
        "--request-json",
        help="Strict reserved-task pilot planning request JSON.",
    ),
) -> None:
    """Freeze and persist an exact non-confirmatory pilot plan without execution."""

    try:
        request = _read_json_object(
            request_json,
            code="pilot_request_invalid",
            label="pilot dry-run request",
        )
        _exact_request_keys(
            request,
            expected={
                "schema_version",
                "pilot_id",
                "epoch",
                "task",
                "audit",
                "session_id",
                "environment_id",
                "environment_digest",
                "tool_calls",
                "evaluator_calls",
                "evidence_paths",
                "created_at_ms",
            },
            code="pilot_request_invalid",
            label="pilot dry-run request",
        )
        if request["schema_version"] != "repo-repair-harness-cli-pilot-dry-run-request-v1":
            raise CliV1Error(
                "pilot_request_invalid",
                "unsupported pilot dry-run request schema",
            )
        if isinstance(request["created_at_ms"], bool) or not isinstance(
            request["created_at_ms"], int
        ):
            raise CliV1Error(
                "pilot_request_invalid",
                "pilot created_at_ms must be an integer",
            )
        root = _canonical_project_root(project_root, create=False)
        pointer_bytes = (root / "active_release.json").read_bytes()
        with _process_role("factory"):
            _assert_evaluator_authority_absent("factory")
            from .contracts.epochs import ResearchEpochManifest, TaskEnvelope
            from .contracts.harness import HarnessProtocol, RuntimeDependencyManifest
            from .contracts.run_evidence import assert_no_resolved_credentials
            from .evaluation.pilot import (
                PilotEvaluatorCall,
                PilotEvidencePath,
                PilotTaskAuditManifest,
                PilotToolCall,
                build_pilot_dry_run_manifest,
            )
            from .factory.harness_release_contracts import (
                ActiveReleasePointer,
                HarnessReleaseManifest,
            )
            from .runtime.api.composite_compiler import compile_composite_run_plan
            from .storage.harness_session_store import HarnessSessionStore

            assert_no_resolved_credentials(request)
            _inspection = _run_source_hidden_entry(
                project_root=root,
                command="inspect",
                timeout_seconds=30.0,
            )
            pointer_payload, generation = _active_release(root)
            try:
                pointer = ActiveReleasePointer.model_validate(pointer_payload)
                release_manifest = HarnessReleaseManifest.model_validate(
                    _read_json_object(
                        generation / "public_release_evidence/release_manifest.json",
                        code="active_release_invalid",
                        label="active harness release manifest",
                    )
                )
                epoch = ResearchEpochManifest.model_validate(request["epoch"])
                task = TaskEnvelope.model_validate(request["task"])
                audit = PilotTaskAuditManifest.model_validate(request["audit"])
                protocol = HarnessProtocol.model_validate(
                    _read_json_object(
                        generation / "runtime/harness_protocol.json",
                        code="active_release_invalid",
                        label="released HarnessProtocol",
                    )
                )
                dependencies = RuntimeDependencyManifest.model_validate(
                    _read_json_object(
                        generation / "runtime/runtime_dependency_manifest.json",
                        code="active_release_invalid",
                        label="released runtime dependency manifest",
                    )
                )
                tool_calls = tuple(
                    PilotToolCall.model_validate(item)
                    for item in request["tool_calls"]
                )
                evaluator_calls = tuple(
                    PilotEvaluatorCall.model_validate(item)
                    for item in request["evaluator_calls"]
                )
                evidence_paths = tuple(
                    PilotEvidencePath.model_validate(item)
                    for item in request["evidence_paths"]
                )
            except CliV1Error:
                raise
            except Exception as exc:
                raise CliV1Error(
                    "pilot_request_invalid",
                    "pilot dry-run request failed strict typed validation",
                ) from exc
            plan = compile_composite_run_plan(task, protocol, dependencies)
            session = HarnessSessionStore(root).load_for_continuation(
                str(request["session_id"]),
                active_release_digest=pointer.release_digest,
            )
            manifest = build_pilot_dry_run_manifest(
                pilot_id=str(request["pilot_id"]),
                active_release=pointer,
                release_manifest=release_manifest,
                epoch=epoch,
                task=task,
                plan=plan,
                audit=audit,
                session_id=session.session_id,
                session_manifest_digest=session.manifest_digest,
                session_release_digest=session.active_release_digest,
                environment_id=str(request["environment_id"]),
                environment_digest=str(request["environment_digest"]),
                tool_calls=tool_calls,
                evaluator_calls=evaluator_calls,
                evidence_paths=evidence_paths,
                created_at_ms=int(request["created_at_ms"]),
            )
            if (
                manifest.live_status != "not_run"
                or manifest.inference_requests_sent != 0
                or any(call.request_sent for call in manifest.model_calls)
                or any(call.call_sent for call in manifest.tool_calls)
                or any(call.call_sent for call in manifest.public_verification_calls)
                or any(call.call_sent for call in manifest.evaluator_calls)
            ):
                raise CliV1Error(
                    "pilot_live_execution_forbidden",
                    "pilot dry run attempted runtime or evaluator execution",
                )
            destination = _controlled_evidence_path(
                root,
                (
                    "controlled_development_and_evaluator_evidence/"
                    f"pilot/{manifest.pilot_id}/dry_run_manifest.json"
                ),
            )
            _write_json_immutable(
                destination,
                manifest.model_dump(mode="json", exclude_none=True),
            )
            _assert_evaluator_authority_absent("factory")
        if (root / "active_release.json").read_bytes() != pointer_bytes:
            raise CliV1Error(
                "pilot_pointer_changed",
                "pilot dry run changed the active release pointer",
            )
        _echo_json(
            {
                "schema_version": CLI_PILOT_DRY_RUN_RESULT_SCHEMA_VERSION,
                "status": "succeeded",
                "operation": "pilot-dry-run",
                "runtime_kind": "harness",
                "live_status": "not_run",
                "real_inference_requests_sent": 0,
                "pilot_id": manifest.pilot_id,
                "manifest_digest": manifest.manifest_digest,
                "planned_model_calls": len(manifest.model_calls),
                "planned_tool_calls": len(manifest.tool_calls),
                "planned_public_verification_calls": len(
                    manifest.public_verification_calls
                ),
                "planned_evaluator_calls": len(manifest.evaluator_calls),
                "provider_calls_sent": 0,
                "evaluator_calls_sent": 0,
                "evidence_path": str(destination),
            }
        )
    except (typer.Exit, KeyboardInterrupt):
        raise
    except Exception as exc:
        _fail("pilot-dry-run", exc)


def _readiness_command(
    *,
    operation: Literal["build", "replay"],
    controlled_root: str,
    request_json: str,
) -> None:
    try:
        root = _canonical_controlled_root(controlled_root)
        request = _controlled_request_file(root, request_json)
        payload = _run_readiness_entry(
            controlled_root=root,
            request_path=request,
        )
        _echo_json(
            _public_readiness_result(
                payload,
                expected_operation=operation,
            )
        )
    except (typer.Exit, KeyboardInterrupt):
        raise
    except Exception as exc:
        _fail(f"readiness-{operation}", exc)


@app.command("readiness-build")
def readiness_build_command(
    controlled_root: str = typer.Argument(
        ...,
        help="Existing controlled evidence workspace containing all readiness inputs.",
    ),
    request_json: str = typer.Option(
        ...,
        "--request-json",
        help="Strict controlled readiness build request JSON inside CONTROLLED_ROOT.",
    ),
) -> None:
    """Build one immutable no-live MVP readiness packet in an evaluator child."""

    _readiness_command(
        operation="build",
        controlled_root=controlled_root,
        request_json=request_json,
    )


@app.command("readiness-replay")
def readiness_replay_command(
    controlled_root: str = typer.Argument(
        ...,
        help="Existing controlled evidence workspace containing the packet generation.",
    ),
    request_json: str = typer.Option(
        ...,
        "--request-json",
        help="Strict controlled readiness replay request JSON inside CONTROLLED_ROOT.",
    ),
) -> None:
    """Revalidate one immutable MVP readiness packet in an evaluator child."""

    _readiness_command(
        operation="replay",
        controlled_root=controlled_root,
        request_json=request_json,
    )


@app.command("inspect")
def inspect_command(
    project_root: str = typer.Argument(
        ...,
        help="Harness factory project directory.",
    ),
) -> None:
    """Inspect the exact active source-hidden harness release."""

    try:
        with _process_role("runtime"):
            _assert_evaluator_authority_absent("runtime")
            root = _canonical_project_root(project_root, create=False)
            inspection = _run_source_hidden_entry(
                project_root=root,
                command="inspect",
                timeout_seconds=30.0,
            )
            _assert_evaluator_authority_absent("runtime")
        _echo_json(
            {
                "schema_version": CLI_INSPECT_RESULT_SCHEMA_VERSION,
                "status": "succeeded",
                "operation": "inspect",
                "runtime_kind": "harness",
                "project_root": str(root),
                "inspection": inspection,
            }
        )
    except (typer.Exit, KeyboardInterrupt):
        raise
    except Exception as exc:
        _fail("inspect", exc)


@app.command("eval")
def eval_command(
    project_root: str = typer.Option(
        ...,
        "--project-root",
        help="Harness factory project whose active release owns authority.",
    ),
    request_json: str = typer.Option(
        ...,
        "--request-json",
        help="Strict evaluator-owned request JSON.",
    ),
    output_json: str = typer.Option(
        ...,
        "--output-json",
        help="Atomic public evaluator result destination.",
    ),
) -> None:
    """Run the evaluator-owned harness entrypoint in a separate authority process."""

    try:
        _assert_unprivileged_launcher()
        _assert_evaluator_authority_absent("CLI evaluator launcher")
        root = _canonical_project_root(project_root, create=False)
        safe_output = _safe_external_output_path(
            project_root=root,
            request_path=request_json,
            output_path=output_json,
        )
        result = _run_evaluator_entry(
            project_root=root,
            request_path=request_json,
            public_output_path=safe_output,
        )
        _assert_evaluator_authority_absent("CLI evaluator launcher")
        payload = {
            "schema_version": CLI_EVAL_RESULT_SCHEMA_VERSION,
            "status": "succeeded",
            "operation": "eval",
            "runtime_kind": "harness",
            "project_root": str(root),
            "result": result,
        }
        _write_json_atomic(safe_output, payload)
        _echo_json(payload)
    except (typer.Exit, KeyboardInterrupt):
        raise
    except Exception as exc:
        payload = _error_payload("eval", exc)
        _echo_json(payload)
        raise typer.Exit(code=2)


def _solve_execution(
    *,
    live: bool,
    provider_manifest: Optional[str],
    command_manifest: Optional[str],
    api_key_file: Optional[str],
) -> dict[str, Any]:
    has_provider_replay = provider_manifest is not None
    has_command_replay = command_manifest is not None
    if live:
        if has_provider_replay or has_command_replay:
            raise CliV1Error(
                "solve_mode_conflict",
                "live solve cannot receive deterministic replay manifests",
            )
        return {"mode": "live"}
    if api_key_file is not None:
        raise CliV1Error(
            "replay_credential_forbidden",
            "deterministic replay cannot receive an API key-file reference",
        )
    if not has_provider_replay or not has_command_replay:
        raise CliV1Error(
            "solve_replay_incomplete",
            "replay solve requires both provider and command replay manifests",
        )
    return {
        "mode": "replay",
        "provider_manifest_path": str(
            _regular_input_path(
                provider_manifest,
                code="provider_replay_invalid",
                label="provider replay manifest",
            )
        ),
        "command_manifest_path": str(
            _regular_input_path(
                command_manifest,
                code="command_replay_invalid",
                label="command replay manifest",
            )
        ),
    }


def _solve_workspace(
    *,
    root: Path,
    workspace: Optional[str],
    run_root: Optional[str],
) -> dict[str, str]:
    if workspace is not None and run_root is not None:
        raise CliV1Error(
            "solve_workspace_conflict",
            "provide at most one explicit workspace or run root",
        )
    if workspace is not None:
        workspace_candidate = Path(workspace).expanduser()
        if workspace_candidate.is_symlink():
            raise CliV1Error(
                "solve_workspace_invalid",
                "run-artifact workspace may not be a symlink",
            )
        return {"run_artifact_workspace": str(workspace_candidate.resolve())}
    run_root_candidate = (
        Path(run_root).expanduser()
        if run_root is not None
        else root / "run_artifacts"
    )
    if run_root_candidate.is_symlink():
        raise CliV1Error(
            "solve_workspace_invalid",
            "run root may not be a symlink",
        )
    selected_root = (
        run_root_candidate.resolve()
    )
    return {"run_root": str(selected_root)}


@app.command("solve")
def solve_command(
    project_root: str = typer.Argument(
        ...,
        help="Harness factory project directory.",
    ),
    task_envelope: str = typer.Option(
        ...,
        "--task-envelope",
        help="Structured public TaskEnvelope JSON.",
    ),
    pair_key: Optional[str] = typer.Option(
        None,
        "--pair-key",
        help="Strict evaluator PairKey JSON for controlled RunEvidence assembly.",
    ),
    replay_provider_manifest: Optional[str] = typer.Option(
        None,
        "--replay-provider-manifest",
        help="Exact deterministic provider replay manifest.",
    ),
    replay_command_manifest: Optional[str] = typer.Option(
        None,
        "--replay-command-manifest",
        help="Exact deterministic isolated-command replay manifest.",
    ),
    live: bool = typer.Option(
        False,
        "--live",
        help="Use the frozen release provider and command policy; no overrides.",
    ),
    api_key_file: Optional[str] = typer.Option(
        None,
        "--api-key-file",
        help="Live-only credential file reference passed through the frozen env-name binding.",
    ),
    workspace: Optional[str] = typer.Option(
        None,
        "--workspace",
        help="New, exclusive run-artifact workspace.",
    ),
    run_root: Optional[str] = typer.Option(
        None,
        "--run-root",
        help="Parent under which the runtime atomically allocates a unique workspace.",
    ),
    run_id: Optional[str] = typer.Option(
        None,
        "--run-id",
        help="Explicit replay execution identity; paired with --workspace-id.",
    ),
    workspace_id: Optional[str] = typer.Option(
        None,
        "--workspace-id",
        help="Explicit replay workspace identity; paired with --run-id.",
    ),
    new_session: bool = typer.Option(
        False,
        "--new-session",
        help="Start a release-pinned bounded runtime session.",
    ),
    session: Optional[str] = typer.Option(
        None,
        "--session",
        help="Continue an existing session pinned to this active release.",
    ),
) -> None:
    """Run one released harness solve in an isolated source-hidden subprocess."""

    role_context = _process_role("runtime")
    role_entered = False
    try:
        role_context.__enter__()
        role_entered = True
        _assert_evaluator_authority_absent("runtime")
        if new_session and session is not None:
            raise CliV1Error(
                "session_mode_conflict",
                "choose either --new-session or --session",
            )
        if not new_session and session is None:
            raise CliV1Error(
                "session_mode_required",
                "harness V1 solve requires --new-session or --session",
            )
        if (run_id is None) != (workspace_id is None):
            raise CliV1Error(
                "execution_identity_incomplete",
                "run-id and workspace-id must be supplied together",
            )
        execution = _solve_execution(
            live=live,
            provider_manifest=replay_provider_manifest,
            command_manifest=replay_command_manifest,
            api_key_file=api_key_file,
        )
        if execution["mode"] == "replay" and run_id is None:
            raise CliV1Error(
                "replay_execution_identity_required",
                "replay solve requires explicit run-id and workspace-id",
            )
        root = _canonical_project_root(project_root, create=False)
        try:
            from .runtime.sdk.harness_release_loader import load_active_harness_release

            release = load_active_harness_release(root)
        except Exception as exc:
            raise CliV1Error(
                "active_release_invalid",
                "active harness release failed strict public validation",
            ) from exc
        try:
            from .authority.public_tasks import load_public_task

            task = load_public_task(
                task_envelope,
                epoch=release.epoch,
                audience="runtime",
            )
            task_source = Path(task_envelope).expanduser().resolve(strict=True)
        except Exception as exc:
            raise CliV1Error(
                "task_envelope_invalid",
                "public task envelope failed strict public loader validation",
            ) from exc
        request: dict[str, Any] = {
            "schema_version": "repo-repair-harness-solve-request-v1",
            "task": task.model_dump(mode="json"),
            "execution": execution,
            **_solve_workspace(root=root, workspace=workspace, run_root=run_root),
        }
        if pair_key is not None:
            pair_key_payload = _read_json_object(
                pair_key,
                code="pair_key_invalid",
                label="evaluator PairKey",
            )
            try:
                from .contracts.outcomes import PairKey

                normalized_pair_key = PairKey.model_validate(pair_key_payload)
            except Exception as exc:
                raise CliV1Error(
                    "pair_key_invalid",
                    "evaluator PairKey failed strict typed validation",
                ) from exc
            if normalized_pair_key.task_manifest_id != task.task_manifest_id:
                raise CliV1Error(
                    "pair_key_task_mismatch",
                    "evaluator PairKey crossed the public task identity",
                )
            request["pair_key"] = normalized_pair_key.model_dump(mode="json")
        if run_id is not None:
            if not _IDENTIFIER_RE.fullmatch(run_id) or not _IDENTIFIER_RE.fullmatch(
                str(workspace_id)
            ):
                raise CliV1Error(
                    "execution_identity_invalid",
                    "run-id and workspace-id must be portable identifiers",
                )
            request["run_id"] = run_id
            request["workspace_id"] = workspace_id

        # Session context/commit is delegated to the typed session boundary.  It is
        # imported only in the product process and never bundled into the runtime.
        from .storage.harness_session_store import HarnessSessionStore

        session_store = HarnessSessionStore(root)
        active_release_digest = str(release.manifest.release_digest)
        if session is None:
            session_manifest = session_store.create_session(
                active_release_digest=active_release_digest,
            )
            session_id = session_manifest.session_id
            session_context = session_store.context_for_next(
                session_id,
                active_release_digest=active_release_digest,
            )
        else:
            session_context = session_store.context_for_next(
                session,
                active_release_digest=active_release_digest,
            )
            session_id = session_context.session_id

        # The runtime entry owns how this bounded public context is admitted.  Keep
        # it typed and separate from the issue instead of synthesizing prompt text.
        request["session_context"] = session_context.to_public_runtime_context().model_dump(
            mode="json"
        )
        try:
            from .repositories.workspaces import resolve_local_snapshot_uri

            snapshot_source = resolve_local_snapshot_uri(
                task.workspace_snapshot.uri,
                relative_to=task_source.parent,
            )
        except Exception as exc:
            raise CliV1Error(
                "task_envelope_invalid",
                "public task workspace snapshot reference is invalid",
            ) from exc
        request["workspace_snapshot_source_path"] = str(snapshot_source)
        solve_result = _run_source_hidden_entry(
            project_root=root,
            command="solve",
            request_payload=request,
            timeout_seconds=max(
                60.0,
                task.ceilings.max_wall_time_ms / 1000.0 + 60.0,
            ),
            api_key_file=api_key_file,
            allow_credentials=execution["mode"] == "live",
        )

        # The SDK result is the evidence authority.  The typed session integration
        # added by F1c translates only its declared public carryover at this point.
        session_message = session_store.append_solve_result(
            session_id,
            active_release_digest=active_release_digest,
            expected_version=session_context.next_sequence,
            task=task,
            solve_result=solve_result,
        )
        _assert_evaluator_authority_absent("runtime")
        _echo_json(
            {
                "schema_version": CLI_SOLVE_RESULT_SCHEMA_VERSION,
                "status": "succeeded",
                "operation": "solve",
                "runtime_kind": "harness",
                "project_root": str(root),
                "session": {
                    "session_id": session_id,
                    "message_id": session_message.message_id,
                    "sequence": session_message.sequence,
                    "active_release_digest": active_release_digest,
                },
                "result": solve_result,
            }
        )
    except (typer.Exit, KeyboardInterrupt):
        raise
    except Exception as exc:
        _fail("solve", exc)
    finally:
        if role_entered:
            role_context.__exit__(None, None, None)


def main() -> None:
    app()


__all__ = [
    "CLI_BUILD_RESULT_SCHEMA_VERSION",
    "CLI_EVAL_RESULT_SCHEMA_VERSION",
    "CLI_ERROR_SCHEMA_VERSION",
    "CLI_GATE0_DRY_RUN_RESULT_SCHEMA_VERSION",
    "CLI_INSPECT_RESULT_SCHEMA_VERSION",
    "CLI_PILOT_DRY_RUN_RESULT_SCHEMA_VERSION",
    "CLI_READINESS_RESULT_SCHEMA_VERSION",
    "CLI_SEARCH_DRY_RUN_RESULT_SCHEMA_VERSION",
    "CLI_SOLVE_RESULT_SCHEMA_VERSION",
    "CliV1Error",
    "app",
    "main",
]


if __name__ == "__main__":
    main()
