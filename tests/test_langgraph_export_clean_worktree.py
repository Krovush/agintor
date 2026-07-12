from __future__ import annotations

import json
from pathlib import Path

from agintor.contracts import baseline_langgraph_runtime_spec
from agintor.runtime.langgraph.compiler import RuntimeSpecCompiler
from agintor.runtime.loader import load_runtime
from agintor.runtime.profile import load_runtime_profile
from agintor.runtime.sdk import KERNEL_BUNDLE_DIR, KERNEL_PACKAGE_NAME
from agintor.runtime.sdk import bundle as kernel_bundle


def test_langgraph_export_clean_worktree_uses_tracked_runtime_profile_default(
    tmp_path: Path,
) -> None:
    source_root = kernel_bundle._package_root()
    tracked_default = source_root / "runtime/sdk/defaults/runtime_profile.json"

    runtime_dir = RuntimeSpecCompiler().compile_to_directory(
        baseline_langgraph_runtime_spec(runtime_id="runtime.clean-worktree"),
        tmp_path / "runtime",
        force=True,
    )

    bundled_profile = (
        runtime_dir
        / KERNEL_BUNDLE_DIR
        / KERNEL_PACKAGE_NAME
        / "runtime/sdk/defaults/runtime_profile.json"
    )
    assert json.loads(bundled_profile.read_text(encoding="utf-8")) == json.loads(
        tracked_default.read_text(encoding="utf-8")
    )

    loaded = load_runtime(
        runtime_dir,
        runtime_profile=load_runtime_profile(),
        runtime_backend="local",
    )
    assert loaded.manifest.runtime_kind == "langgraph_spec"
    assert loaded.runtime_spec.spec_digest == loaded.manifest.runtime_spec_digest
