from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .prompts import load_prompt_spec
from .runtime_profile import load_runtime_profile
from .runtime_loader import load_runtime
METHOD_CONTRACTS = {
    "top": ["score_agent", "select_mode", "propose_children", "select_workers", "assign_scope", "merge_ensemble", "make_checkpoint"],
    "mem": ["select_spans_for_compaction", "summarize_span", "retrieve_long_term", "score_memory_unit", "should_promote", "dedup_candidates", "upsert_memory"],
    "tool": ["rank_categories", "rank_tools", "should_create_tool", "propose_tool_spec", "validate_tool", "promote_tool", "dispatch_tool"],
    "ctl": ["assign_model", "request_checks", "stop_policy", "score_interface_scope", "update_scope_credit"],
}


def build_mutation_prompt(
    runtime_dir: Path,
    objective: str,
    touched_scope: Sequence[str],
    predictor_summaries: Mapping[str, object],
    failing_train_traces: Sequence[dict[str, object]],
    exemplars: Sequence[dict[str, object]],
    runtime_profile: object | None = None,
) -> str:
    runtime = load_runtime(runtime_dir)
    profile = runtime_profile or load_runtime_profile(runtime_dir)
    spec = load_prompt_spec(profile.prompts.mutation_patch)
    files_text = {}
    for rel_path in runtime.manifest.mutable_files:
        files_text[rel_path] = (runtime_dir / rel_path).read_text(encoding="utf-8")
    contracts = {scope: METHOD_CONTRACTS[scope] for scope in touched_scope}
    exemplar_rows = list(exemplars)[:6]
    payload = {
        "prompt_id": spec.prompt_id,
        "objective": objective,
        "touched_scope": list(touched_scope),
        "mutable_files": files_text,
        "immutable_manifest": runtime.manifest.immutable_manifest,
        "contracts": contracts,
        "predictor_summaries": predictor_summaries,
        "recent_failing_train_traces": list(failing_train_traces),
        "high_performing_exemplars": exemplar_rows,
        "patch_rules": {
            "format": "SEARCH/REPLACE only",
            "max_blocks": 4,
            "max_changed_lines": 60,
            "max_search_lines": 8,
        },
    }
    return json.dumps(payload, indent=2, sort_keys=True)
