from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agintor.contracts import GoalSpec, baseline_langgraph_runtime_spec
from agintor.factory.export import _write_seed_runtime
from agintor.oracle.compiler import OracleCompiler
from agintor.oracle.package_io import write_oracle_package
from agintor.runtime.loader import load_runtime
from agintor.runtime.profile import load_runtime_profile


FORBIDDEN_KEYS = {
    "private_expected",
    "sealed_inputs",
    "sealed_fixture_refs",
    "hidden_tests",
    "promotion_threshold",
    "private_rubric",
}
FORBIDDEN_FILE_NAMES = {"sealed.json", "hidden_tests.json", "private_rubric.json"}


def _goal() -> GoalSpec:
    return GoalSpec(
        goal_id="goal.export-audit",
        raw_prompt="build a validation-backed runtime",
        normalized_goal="build a validation-backed runtime",
        constraints={"runtime_kind": "langgraph_spec"},
    )


def _forbidden_json_key_paths(value, *, path: str = "$") -> list[str]:
    if isinstance(value, dict):
        paths: list[str] = []
        for key, item in value.items():
            child_path = f"{path}.{key}"
            if key in FORBIDDEN_KEYS:
                paths.append(child_path)
            paths.extend(_forbidden_json_key_paths(item, path=child_path))
        return paths
    if isinstance(value, list):
        paths: list[str] = []
        for index, item in enumerate(value):
            paths.extend(_forbidden_json_key_paths(item, path=f"{path}[{index}]"))
        return paths
    return []


def _json_payloads(path: Path):
    if path.suffix == ".json":
        yield json.loads(path.read_text(encoding="utf-8"))
    elif path.suffix == ".jsonl":
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                yield json.loads(line)


def test_export_no_sealed_material_copies_only_public_oracle_projection(tmp_path: Path) -> None:
    goal = _goal()
    spec = baseline_langgraph_runtime_spec(runtime_id="runtime.export-audit")
    package = write_oracle_package(OracleCompiler().compile(goal, spec), tmp_path / "oracle-package")
    profile = load_runtime_profile()
    runtime_plan = SimpleNamespace(
        plan_id="runtime.export-audit",
        runtime_kind="langgraph_spec",
        runtime_profile=profile.model_dump(mode="json"),
        oracle_package_hash=package.package_hash,
        oracle_public_ref=str(tmp_path / "oracle-package" / "public.json"),
        oracle_public_view_hash=package.public_view_hash,
    )

    runtime_dir = tmp_path / "exported-runtime"
    _write_seed_runtime(
        runtime_dir,
        runtime_plan,
        goal_spec=goal,
        runtime_profile=profile,
        runtime_backend="local",
    )

    forbidden_files = [
        path.relative_to(runtime_dir).as_posix()
        for path in runtime_dir.rglob("*")
        if path.is_file() and path.name in FORBIDDEN_FILE_NAMES
    ]
    forbidden_keys: dict[str, list[str]] = {}
    for path in runtime_dir.rglob("*"):
        if not path.is_file() or path.suffix not in {".json", ".jsonl"}:
            continue
        matches: list[str] = []
        for payload in _json_payloads(path):
            matches.extend(_forbidden_json_key_paths(payload))
        if matches:
            forbidden_keys[path.relative_to(runtime_dir).as_posix()] = matches

    loaded = load_runtime(runtime_dir, runtime_profile=profile, runtime_backend="local")

    assert (runtime_dir / "oracle/public.json").is_file()
    assert not (runtime_dir / "oracle/sealed.json").exists()
    assert loaded.manifest.oracle_package_hash == package.package_hash
    assert forbidden_files == []
    assert forbidden_keys == {}
