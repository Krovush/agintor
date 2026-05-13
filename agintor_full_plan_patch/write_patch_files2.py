from __future__ import annotations
from pathlib import Path
import textwrap
ROOT = Path('/mnt/data/agintor_full_plan_patch/new_files')
files = {}
def add(path, content): files[path]=textwrap.dedent(content).lstrip()

add('agintor/oracle/__init__.py', r'''
from __future__ import annotations

from .package_io import *  # noqa: F401,F403
from .projections import *  # noqa: F401,F403
from .qa import *  # noqa: F401,F403
from .compiler import *  # noqa: F401,F403
from .validator_registry import *  # noqa: F401,F403
''')

add('agintor/oracle/package_io.py', r'''
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping

from ..contracts import OraclePackage
from ..utils import ensure_directory, stable_hash
from .projections import public_oracle_projection, sealed_oracle_projection

ORACLE_PACKAGE_FILE = "oracle_package.json"
ORACLE_PUBLIC_FILE = "oracle_public.json"
ORACLE_SEALED_FILE = "oracle_sealed.json"
ORACLE_LOCK_FILE = "oracle_package.lock.json"


def canonical_jsonable(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return canonical_jsonable(value.model_dump(mode="json", exclude_none=True))
    if isinstance(value, Mapping):
        return {str(key): canonical_jsonable(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [canonical_jsonable(item) for item in value]
    return value


def canonical_json(value: Any) -> str:
    return json.dumps(canonical_jsonable(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_oracle_package_hash(package: OraclePackage | Mapping[str, Any]) -> str:
    payload = package.model_dump(mode="json", exclude_none=True) if isinstance(package, OraclePackage) else dict(package)
    payload.pop("package_hash", None)
    payload.pop("public_view_hash", None)
    payload.pop("sealed_view_hash", None)
    return stable_hash(canonical_jsonable(payload))


def finalize_oracle_package(package: OraclePackage) -> OraclePackage:
    public_payload = public_oracle_projection(package)
    sealed_payload = sealed_oracle_projection(package)
    public_hash = stable_hash(canonical_jsonable(public_payload))
    sealed_hash = stable_hash(canonical_jsonable(sealed_payload))
    package_hash = compute_oracle_package_hash(
        package.model_copy(update={"public_view_hash": public_hash, "sealed_view_hash": sealed_hash})
    )
    return package.model_copy(
        update={
            "package_hash": package_hash,
            "public_view_hash": public_hash,
            "sealed_view_hash": sealed_hash,
            "frozen": True,
        },
        deep=True,
    )


def write_oracle_package(package: OraclePackage, package_dir: str | Path) -> OraclePackage:
    frozen = finalize_oracle_package(package)
    root = ensure_directory(Path(package_dir))
    (root / ORACLE_PACKAGE_FILE).write_text(json.dumps(frozen.model_dump(mode="json", exclude_none=True), indent=2, sort_keys=True), encoding="utf-8")
    (root / ORACLE_PUBLIC_FILE).write_text(json.dumps(public_oracle_projection(frozen), indent=2, sort_keys=True), encoding="utf-8")
    (root / ORACLE_SEALED_FILE).write_text(json.dumps(sealed_oracle_projection(frozen), indent=2, sort_keys=True), encoding="utf-8")
    lock = {
        "package_id": frozen.package_id,
        "package_hash": frozen.package_hash,
        "public_view_hash": frozen.public_view_hash,
        "sealed_view_hash": frozen.sealed_view_hash,
        "goal_id": frozen.goal_id,
        "runtime_spec_digest": frozen.runtime_spec_digest,
    }
    (root / ORACLE_LOCK_FILE).write_text(json.dumps(lock, indent=2, sort_keys=True), encoding="utf-8")
    return frozen


def load_oracle_package(path: str | Path) -> OraclePackage:
    source = Path(path)
    package_path = source / ORACLE_PACKAGE_FILE if source.is_dir() else source
    return OraclePackage.model_validate(json.loads(package_path.read_text(encoding="utf-8")))


def assert_package_lock_matches(package: OraclePackage, package_dir: str | Path) -> None:
    lock_path = Path(package_dir) / ORACLE_LOCK_FILE
    if not lock_path.exists():
        raise ValueError(f"missing oracle package lockfile at {lock_path}")
    lock = json.loads(lock_path.read_text(encoding="utf-8"))
    expected = finalize_oracle_package(package)
    for key in ("package_hash", "public_view_hash", "sealed_view_hash"):
        if str(lock.get(key, "")) != str(getattr(expected, key)):
            raise ValueError(f"oracle package lock mismatch for {key}")


__all__ = [
    "ORACLE_LOCK_FILE",
    "ORACLE_PACKAGE_FILE",
    "ORACLE_PUBLIC_FILE",
    "ORACLE_SEALED_FILE",
    "assert_package_lock_matches",
    "canonical_json",
    "canonical_jsonable",
    "compute_oracle_package_hash",
    "finalize_oracle_package",
    "load_oracle_package",
    "write_oracle_package",
]
''')

add('agintor/oracle/projections.py', r'''
from __future__ import annotations

from typing import Any, Mapping

from ..contracts import OraclePackage

_PRIVATE_KEYS = {
    "sealed_inputs",
    "sealed_fixture_refs",
    "private_expected",
    "private_answer",
    "private_answer_ref",
    "hidden_tests",
    "promotion_thresholds",
    "private_rubric",
}
_PRIVATE_PREFIXES = ("private_", "sealed_", "hidden_")


def _strip_private(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _strip_private(value.model_dump(mode="json", exclude_none=True))
    if isinstance(value, Mapping):
        stripped: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            normalized = key_text.lower()
            if normalized in _PRIVATE_KEYS or any(normalized.startswith(prefix) for prefix in _PRIVATE_PREFIXES):
                continue
            stripped[key_text] = _strip_private(item)
        return stripped
    if isinstance(value, list):
        return [_strip_private(item) for item in value]
    return value


def _private_paths(value: Any, *, path: str = "") -> list[str]:
    paths: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            child_path = f"{path}.{key_text}" if path else key_text
            normalized = key_text.lower()
            if normalized in _PRIVATE_KEYS or any(normalized.startswith(prefix) for prefix in _PRIVATE_PREFIXES):
                paths.append(child_path)
            paths.extend(_private_paths(item, path=child_path))
    elif isinstance(value, list):
        for idx, item in enumerate(value):
            paths.extend(_private_paths(item, path=f"{path}[{idx}]"))
    return paths


def public_oracle_projection(package: OraclePackage) -> dict[str, Any]:
    payload = package.model_dump(mode="json", exclude_none=True)
    for validator in payload.get("validator_specs", []):
        if validator.get("visibility") in {"private", "sealed"}:
            validator["inputs"] = {}
            validator["outputs_schema"] = {}
            validator["health_tests"] = []
    payload["task_sets"] = [
        {
            **task_set,
            "tasks": [
                {
                    key: _strip_private(value)
                    for key, value in task.items()
                    if key not in {"sealed_inputs", "sealed_fixture_refs"}
                }
                for task in task_set.get("tasks", [])
            ],
        }
        for task_set in payload.get("task_sets", [])
    ]
    payload["fixture_bundle_refs"] = [
        ref for ref in payload.get("fixture_bundle_refs", []) if ref.get("visibility") == "public"
    ]
    return _strip_private(payload)


def sealed_oracle_projection(package: OraclePackage) -> dict[str, Any]:
    return package.model_dump(mode="json", exclude_none=True)


def assert_no_private_oracle_fields(value: Any) -> None:
    paths = _private_paths(value)
    if paths:
        raise ValueError(f"private oracle fields leaked into public view: {paths}")


def public_task_views(package: OraclePackage, partition: str = "train") -> list[dict[str, Any]]:
    views: list[dict[str, Any]] = []
    public = public_oracle_projection(package)
    for task_set in public.get("task_sets", []):
        if str(task_set.get("partition", "")) != partition:
            continue
        views.extend(list(task_set.get("tasks", [])))
    return views


__all__ = [
    "assert_no_private_oracle_fields",
    "public_oracle_projection",
    "public_task_views",
    "sealed_oracle_projection",
]
''')

add('agintor/oracle/qa.py', r'''
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from ..contracts import OraclePackage
from .package_io import finalize_oracle_package
from .projections import public_oracle_projection, assert_no_private_oracle_fields


class OracleQAIssue(BaseModel):
    issue_id: str
    severity: Literal["error", "warning", "info"] = "error"
    message: str


class OracleQAReport(BaseModel):
    package_id: str
    package_hash: str = ""
    passed: bool = False
    issues: list[OracleQAIssue] = Field(default_factory=list)

    @property
    def errors(self) -> list[OracleQAIssue]:
        return [issue for issue in self.issues if issue.severity == "error"]


def run_oracle_qa(package: OraclePackage) -> OracleQAReport:
    issues: list[OracleQAIssue] = []

    def add(issue_id: str, message: str, severity: Literal["error", "warning", "info"] = "error") -> None:
        issues.append(OracleQAIssue(issue_id=issue_id, severity=severity, message=message))

    frozen = finalize_oracle_package(package)
    if not package.frozen:
        add("package.not_frozen", "oracle package must be frozen before candidate evaluation")
    claim_ids = {claim.claim_id for claim in package.claim_graph.claims}
    validator_claims = {claim_id for validator in package.validator_specs for claim_id in validator.claim_ids}
    for claim in package.claim_graph.claims:
        if claim.criticality in {"hard", "major"} and claim.claim_id not in validator_claims and not claim.unverifiable_reason:
            add(
                f"claim.{claim.claim_id}.missing_validator",
                f"critical claim {claim.claim_id!r} has no validator and no explicit unverifiable reason",
            )
    for obligation in package.proof_obligations:
        missing = sorted(set(obligation.claim_ids) - claim_ids)
        if missing:
            add(f"obligation.{obligation.obligation_id}.missing_claims", f"proof obligation references missing claims: {missing}")
        if not obligation.validator_family_hints:
            add(f"obligation.{obligation.obligation_id}.no_family_hint", "proof obligation has no validator family hints", "warning")
    validator_ids = [validator.validator_id for validator in package.validator_specs]
    if len(validator_ids) != len(set(validator_ids)):
        add("validators.duplicate_ids", "validator ids must be unique")
    if not package.validator_specs:
        add("validators.empty", "oracle package requires at least one validator")
    if not package.task_sets:
        add("tasks.empty", "oracle package requires at least one task set")
    try:
        public_view = public_oracle_projection(frozen)
        assert_no_private_oracle_fields(public_view)
    except Exception as exc:
        add("projection.private_leakage", str(exc))
    if package.authority_policy.allow_model_judge_promotion_alone:
        add("authority.weak_judge_promotion", "model judges should not be final promotion authority alone", "warning")
    if package.evidence_contract.contract_id == "":
        add("evidence_contract.missing_id", "evidence contract id is required")
    return OracleQAReport(
        package_id=package.package_id,
        package_hash=frozen.package_hash,
        passed=not any(issue.severity == "error" for issue in issues),
        issues=issues,
    )


def assert_oracle_qa_passes(package: OraclePackage) -> OracleQAReport:
    report = run_oracle_qa(package)
    if not report.passed:
        rendered = "; ".join(f"{issue.issue_id}: {issue.message}" for issue in report.errors)
        raise ValueError(f"oracle QA failed: {rendered}")
    return report


__all__ = ["OracleQAIssue", "OracleQAReport", "assert_oracle_qa_passes", "run_oracle_qa"]
''')

for path, content in files.items():
    target = ROOT / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding='utf-8')
print(f'wrote {len(files)} files')
