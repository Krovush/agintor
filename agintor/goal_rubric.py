from __future__ import annotations

import re
from typing import Any, Iterable

from .schemas import GoalSpec, SuccessCriteriaBundle, SuccessCriterion
from .utils import stable_hash


_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_STOPWORDS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "be",
    "build",
    "by",
    "for",
    "from",
    "how",
    "in",
    "into",
    "is",
    "of",
    "on",
    "or",
    "should",
    "specialized",
    "system",
    "that",
    "the",
    "this",
    "to",
    "use",
    "using",
    "with",
}
_GENERIC_GOAL_TERMS = {
    "agent",
    "agents",
    "builder",
    "building",
    "capability",
    "capabilities",
    "constructor",
    "constructors",
    "core",
    "runtime",
    "runtimes",
    "shell",
}
_FAMILY_ORDER = ("e2e", "top", "mem", "tool")
_FAMILY_HINTS: dict[str, set[str]] = {
    "top": {
        "coordination",
        "decompose",
        "delegation",
        "ensemble",
        "merge",
        "multiagent",
        "orchestrate",
        "orchestration",
        "planner",
        "planning",
        "topology",
        "worker",
        "workflow",
    },
    "mem": {
        "backlinks",
        "checkpoint",
        "checkpoints",
        "context",
        "history",
        "knowledge",
        "memory",
        "persist",
        "resume",
        "retrieval",
        "retrieve",
        "state",
    },
    "tool": {
        "browser",
        "container",
        "containers",
        "dependency",
        "execution",
        "reuse",
        "sandbox",
        "tool",
        "tooling",
        "tools",
    },
    "e2e": {
        "artifact",
        "deliverable",
        "end",
        "product",
        "report",
        "system",
        "workflow",
    },
}

_CAPABILITY_HINTS: dict[str, set[str]] = {
    "decomposition_orchestration": {"decompose", "delegation", "orchestrate", "orchestration", "planner", "workflow", "multiagent"},
    "memory_retrieval": {"memory", "retrieve", "retrieval", "resume", "checkpoint", "history", "knowledge", "context"},
    "tool_reuse_synthesis": {"tool", "tooling", "tools", "sandbox", "container", "execution", "reuse"},
    "artifact_generation": {"artifact", "deliverable", "report", "export", "deploy", "deployment"},
    "verification_heavy_solving": {"verify", "verification", "deterministic", "exact", "correctness", "validated"},
    "cost_sensitive_solving": {"budget", "cheap", "cost", "costs", "efficient"},
    "latency_sensitive_solving": {"fast", "latency", "quick", "responsive"},
    "checkpointed_workflows": {"checkpoint", "resume", "resumable", "recovery"},
}

_DEFAULT_CAPABILITIES_BY_FAMILY: dict[str, list[str]] = {
    "top": ["decomposition_orchestration"],
    "mem": ["memory_retrieval", "checkpointed_workflows"],
    "tool": ["tool_reuse_synthesis"],
    "e2e": ["artifact_generation", "verification_heavy_solving"],
}


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for raw_value in values:
        value = " ".join(str(raw_value or "").split()).strip()
        if not value:
            continue
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(value)
    return ordered


def canonical_goal_prompt(goal_prompt: str) -> str:
    return " ".join(str(goal_prompt or "").split()).strip()


def normalized_goal_terms(text: str) -> list[str]:
    return [match.group(0).lower() for match in _WORD_RE.finditer(text)]


def extract_goal_keywords(goal_prompt: str, *, max_keywords: int = 6) -> list[str]:
    terms = normalized_goal_terms(goal_prompt)
    filtered = [
        term
        for term in terms
        if len(term) > 2 and term not in _STOPWORDS and term not in _GENERIC_GOAL_TERMS
    ]
    if not filtered:
        filtered = [term for term in terms if len(term) > 2 and term not in _STOPWORDS]
    return _dedupe(filtered)[:max_keywords]


def extract_goal_phrases(goal_prompt: str, *, max_phrases: int = 4) -> list[str]:
    keywords = extract_goal_keywords(goal_prompt, max_keywords=max(3, max_phrases + 1))
    phrases: list[str] = []
    if len(keywords) >= 2:
        phrases.extend(" ".join(keywords[index : index + 2]) for index in range(len(keywords) - 1))
    if len(keywords) >= 3:
        phrases.append(" ".join(keywords[-3:]))
    return _dedupe(phrases)[:max_phrases]


def derive_goal_families(goal_prompt: str, *, max_families: int = 2) -> list[str]:
    terms = set(normalized_goal_terms(goal_prompt))
    keywords = set(extract_goal_keywords(goal_prompt))
    scores = {
        family: len(terms & hints) + len(keywords & hints)
        for family, hints in _FAMILY_HINTS.items()
    }
    ranked = sorted(
        scores.items(),
        key=lambda item: (-item[1], _FAMILY_ORDER.index(item[0])),
    )
    selected = [family for family, score in ranked if score > 0][:max_families]
    if not selected:
        return ["e2e", "top"][:max_families]
    if "e2e" not in selected and len(selected) < max_families:
        selected.append("e2e")
    return _dedupe(selected)[:max_families]


def derive_goal_expectations(
    goal_prompt: str,
    *,
    default_sections: Iterable[str] = (),
    default_phrases: Iterable[str] = (),
) -> dict[str, object]:
    goal_keywords = extract_goal_keywords(goal_prompt)
    goal_phrases = extract_goal_phrases(goal_prompt)
    required_phrases = _dedupe([*goal_keywords[:4], *goal_phrases[:2], *list(default_phrases)])
    return {
        "goal_prompt": goal_prompt,
        "goal_keywords": goal_keywords,
        "goal_phrases": goal_phrases,
        "required_phrases": required_phrases,
        "target_families": derive_goal_families(goal_prompt),
    }


def _runtime_backend_preference(goal_prompt: str) -> str | None:
    prompt = canonical_goal_prompt(goal_prompt).lower()
    if "docker" in prompt or "container" in prompt:
        return "docker"
    if "local" in prompt or "offline" in prompt:
        return "local"
    return None


def _provider_preferences(goal_prompt: str) -> list[str]:
    prompt = canonical_goal_prompt(goal_prompt).lower()
    providers: list[str] = []
    if "openai" in prompt:
        providers.append("openai")
    if "minimax" in prompt:
        providers.append("minimax")
    if "local" in prompt or "offline" in prompt or "deterministic" in prompt:
        providers.append("local")
    return _dedupe(providers)


def _required_capabilities(goal_prompt: str, target_families: Iterable[str]) -> list[str]:
    terms = set(normalized_goal_terms(goal_prompt))
    capabilities: list[str] = []
    for capability, hints in _CAPABILITY_HINTS.items():
        if terms & hints:
            capabilities.append(capability)
    for family in target_families:
        capabilities.extend(_DEFAULT_CAPABILITIES_BY_FAMILY.get(family, []))
    if not capabilities:
        capabilities.extend(["artifact_generation", "verification_heavy_solving"])
    return _dedupe(capabilities)


def _goal_constraints(
    goal_prompt: str,
    target_families: Iterable[str],
    runtime_provider_name: str | None,
    runtime_backend: str,
) -> tuple[dict[str, Any], list[str]]:
    prompt = canonical_goal_prompt(goal_prompt).lower()
    assumptions: list[str] = []
    provider_preferences = _provider_preferences(goal_prompt)
    constraints: dict[str, Any] = {
        "provider_preferences": provider_preferences,
        "runtime_backend": runtime_backend,
        "network_policy": "restricted" if "offline" in prompt or "no network" in prompt else "provider-only",
        "filesystem_policy": "workspace-read-write",
        "target_families": list(target_families),
    }
    if runtime_provider_name:
        constraints["runtime_provider"] = runtime_provider_name
        if not provider_preferences:
            assumptions.append(
                f"No runtime provider was requested explicitly; defaulting to the configured runtime provider {runtime_provider_name}."
            )
    if not provider_preferences:
        assumptions.append("No hosted-provider preference was specified; all configured provider classes remain allowed.")
    return constraints, assumptions


def build_goal_spec(
    goal_prompt: str,
    *,
    runtime_provider_name: str | None = None,
    default_runtime_backend: str | None = None,
) -> GoalSpec:
    normalized_goal = canonical_goal_prompt(goal_prompt)
    goal_keywords = extract_goal_keywords(normalized_goal)
    goal_phrases = extract_goal_phrases(normalized_goal)
    target_families = derive_goal_families(normalized_goal)
    required_capabilities = _required_capabilities(normalized_goal, target_families)
    prompt_runtime_backend = _runtime_backend_preference(normalized_goal)
    effective_runtime_backend = (
        str(default_runtime_backend).strip().lower()
        if default_runtime_backend and str(default_runtime_backend).strip()
        else prompt_runtime_backend or "local"
    )
    constraints, assumptions = _goal_constraints(
        normalized_goal,
        target_families,
        runtime_provider_name,
        effective_runtime_backend,
    )
    if prompt_runtime_backend is None and not default_runtime_backend:
        assumptions.append("No runtime backend was specified; defaulting to local execution for the build plan.")
    deployment_preferences = {
        "runtime_backend": effective_runtime_backend,
        "export_format": "directory",
        "supported_backends": ["local", "docker"],
    }
    success_criteria = [
        f"Demonstrate measurable pressure on the {family} family."
        for family in target_families
    ]
    if "verification_heavy_solving" in required_capabilities:
        success_criteria.append("Prefer exact local verification when a deterministic checker is available.")
    if "artifact_generation" in required_capabilities:
        success_criteria.append("Produce a runtime artifact that remains runnable after export.")
    if "cost_sensitive_solving" in required_capabilities:
        success_criteria.append("Keep solve-time cost within bounded execution budgets.")
    if "latency_sensitive_solving" in required_capabilities:
        success_criteria.append("Prefer lower-latency execution paths when quality is comparable.")
    return GoalSpec(
        goal_id=f"goal.{stable_hash(normalized_goal)[:12]}",
        raw_prompt=str(goal_prompt),
        normalized_goal=normalized_goal,
        goal_keywords=goal_keywords,
        goal_phrases=goal_phrases,
        required_capabilities=required_capabilities,
        constraints=constraints,
        success_criteria=_dedupe(success_criteria),
        target_families=target_families,
        deployment_preferences=deployment_preferences,
        assumptions=_dedupe(assumptions),
    )


def build_success_criteria_bundle(goal_spec: GoalSpec) -> SuccessCriteriaBundle:
    criteria: list[SuccessCriterion] = []
    family_weights = {"top": 1.0, "mem": 1.0, "tool": 1.0, "e2e": 1.2}
    for family in goal_spec.target_families:
        criteria.append(
            SuccessCriterion(
                criterion_id=f"{goal_spec.goal_id}.{family}",
                description=f"Improve verifier-backed performance on {family} tasks aligned with the normalized goal.",
                required=True,
                priority=1,
                measurable_signal=f"family mean utility for {family}",
                verifier_hint=f"Use the frozen {family} benchmark verifiers for scoring.",
                target_family=family,
                weight=family_weights.get(family, 1.0),
            )
        )
    if "verification_heavy_solving" in goal_spec.required_capabilities:
        criteria.append(
            SuccessCriterion(
                criterion_id=f"{goal_spec.goal_id}.verification",
                description="Surface a verified terminal artifact whenever an exact verifier exists for the adapted task.",
                required=True,
                priority=1,
                measurable_signal="benchmark verifier score and executed checker ladder",
                verifier_hint="Prefer benchmark checks for irreversible or externally visible artifacts.",
                target_family="e2e",
                weight=1.1,
            )
        )
    if "cost_sensitive_solving" in goal_spec.required_capabilities:
        criteria.append(
            SuccessCriterion(
                criterion_id=f"{goal_spec.goal_id}.cost",
                description="Keep solve-time cost pressure bounded relative to baseline reference scales.",
                required=False,
                priority=2,
                measurable_signal="utility penalty on cost",
                verifier_hint="Track budget consumption and cost-adjusted utility.",
                target_family="e2e",
                weight=0.6,
            )
        )
    if "latency_sensitive_solving" in goal_spec.required_capabilities:
        criteria.append(
            SuccessCriterion(
                criterion_id=f"{goal_spec.goal_id}.latency",
                description="Keep solve-time latency bounded relative to baseline reference scales.",
                required=False,
                priority=2,
                measurable_signal="utility penalty on latency",
                verifier_hint="Track wall-clock runtime and latency-adjusted utility.",
                target_family="e2e",
                weight=0.6,
            )
        )
    return SuccessCriteriaBundle(
        bundle_id=f"criteria.{stable_hash(goal_spec.goal_id, goal_spec.success_criteria)[:12]}",
        goal_id=goal_spec.goal_id,
        criteria=criteria,
        assumptions=list(goal_spec.assumptions),
    )


def amend_goal_spec(
    prior_goal: GoalSpec,
    instruction: str,
    *,
    runtime_provider_name: str | None = None,
    default_runtime_backend: str | None = None,
) -> GoalSpec:
    """Amend a prior `GoalSpec` with a follow-up instruction.

    The amended goal preserves the original `goal_id` so the chat keeps a stable
    identity across follow-ups; the prior goal text and the new instruction are
    combined into the raw prompt and re-canonicalized to derive updated keywords,
    phrases, target families, capabilities, constraints, and success criteria.
    `amendment_index` is bumped and `amendment_history` is extended with the new
    instruction.
    """

    instruction_text = canonical_goal_prompt(instruction)
    if not instruction_text:
        raise ValueError("amendment instruction may not be empty")
    combined_prompt = f"{prior_goal.normalized_goal}\n\nFollow-up: {instruction_text}".strip()
    refreshed = build_goal_spec(
        combined_prompt,
        runtime_provider_name=runtime_provider_name,
        default_runtime_backend=default_runtime_backend,
    )
    history: list[str] = list(prior_goal.amendment_history)
    history.append(instruction_text)
    return (refreshed).model_copy(
        update={
            "goal_id": prior_goal.goal_id,
            "raw_prompt": instruction_text,
            "amendment_index": int(prior_goal.amendment_index) + 1,
            "amendment_history": history,
        }
    )
