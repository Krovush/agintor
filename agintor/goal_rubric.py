from __future__ import annotations

import re
from typing import Iterable


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
        "build",
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
