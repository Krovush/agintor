from __future__ import annotations

import json
from importlib import resources
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from .pydantic_compat import model_dump, model_validate


RUNTIME_PROFILE_FILE = "runtime_profile.json"


class PromptProfile(BaseModel):
    mutation_patch: str = "evolve.mutator_patch.v1"
    memory_summary: str = "memory.span_summarize.v1"
    tool_spec: str = "tool.spec_generate.v1"


class HostedProviderProfile(BaseModel):
    name: str = "minimax"
    base_url: str | None = None
    base_url_env: str | None = None
    api_key_env: str | None = None
    api_key_file_env: str | None = None
    model_map: dict[str, str] = Field(default_factory=dict)
    reasoning_effort_map: dict[str, str] = Field(default_factory=dict)
    temperature: float | None = None
    pricing_map: dict[str, dict[str, float]] = Field(default_factory=dict)
    pricing_env: str | None = None


class ExecutionProfile(BaseModel):
    max_steps: int = 64
    cost_max: float = 100.0
    latency_max: float = 120.0
    model_calls_max: int = 64
    checks_max: int = 16
    context_window_tokens: int = 768


class EvaluationProfile(BaseModel):
    reference_scale_seeds: list[int] = Field(default_factory=lambda: [0])
    proxy_seeds: list[int] = Field(default_factory=lambda: [0])
    subset_seeds: list[int] = Field(default_factory=lambda: [0])
    full_train_seeds: list[int] = Field(default_factory=lambda: [0, 1, 2])
    validation_seeds: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4])
    stage1_replays: int = 2
    epsilon_proxy: float = 0.01
    epsilon_part: float = 0.01
    epsilon_full: float = 0.05
    stage4_minibatch_size: int = 4
    delta_rej: float = 0.01
    family_weights: dict[str, float] = Field(default_factory=lambda: {"top": 0.25, "mem": 0.25, "tool": 0.25, "e2e": 0.25})
    lambdas: dict[str, float] = Field(default_factory=lambda: {"cost": 0.08, "latency": 0.05, "fault": 0.12})
    robustness: dict[str, float] = Field(default_factory=lambda: {"eta_sigma": 0.35, "kappa_b": 0.25, "kappa_u": 0.30, "alpha": 1.0 / 3.0})
    pass_rate_caps: dict[str, float] = Field(default_factory=lambda: {"stage1": 0.35, "stage2": 0.15, "stage3": 0.05})


class EvolutionProfile(BaseModel):
    phase_budgets: dict[str, int] = Field(default_factory=lambda: {"local": 1200, "pair": 600, "joint": 300})
    crossover_probability: float = 0.15


class TopologyProfile(BaseModel):
    theta_create: float = 0.58
    k_max: int = 3
    spawn_penalty: float = 0.06
    coord_penalty: float = 0.05
    dep_penalty: float = 0.04
    size_penalty: float = 0.03
    conflict_penalty: float = 0.03
    coldstart_penalty: float = 0.02


class MemoryProfile(BaseModel):
    b_hi: float = 0.75
    b_lo: float = 0.55
    theta_e: float = 0.92
    theta_l: float = 0.60
    theta_prom: float = 0.55
    eta_verify: float = 0.50
    max_summaries_per_pass: int = 3
    token_window: float = 512.0
    compaction_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "retained_utility": 0.55,
            "event_bonus": 0.10,
            "verifier_bonus": 0.05,
            "token_saving": 0.04,
            "info_loss": 0.15,
            "latency": 0.05,
            "orphan": 0.20,
        }
    )
    retrieval_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "cos": 0.30,
            "lex": 0.20,
            "type": 0.15,
            "path": 0.10,
            "recency": 0.10,
            "verify": 0.10,
            "provenance": 0.05,
            "staleness": 0.05,
            "exact_path_bonus": 0.25,
            "exact_verify_bonus": 0.20,
            "exact_task_context_bonus": 0.10,
        }
    )
    promotion_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "novelty": 1.2,
            "reuse": 0.9,
            "centrality": 0.8,
            "verifier": 1.0,
            "task_spread": 0.5,
            "compositional": 0.6,
            "duplicate": 1.0,
            "write_cost": 0.4,
            "contradiction": 0.8,
        }
    )


class ToolingProfile(BaseModel):
    eta_p: float = 0.80
    eta_r: int = 3
    k_c: int = 3
    t_slice: float = 60.0
    build_weight: float = 0.20
    exec_weight: float = 0.10
    safety_weight: float = 0.05
    future_weight: float = 0.10
    category_ranking_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "sim": 0.35,
            "iface": 0.20,
            "histpass": 0.10,
            "cachehit": 0.10,
            "descendants": 0.08,
            "coldstart": 0.10,
            "permrisk": 0.07,
        }
    )
    tool_ranking_weights: dict[str, float] = Field(
        default_factory=lambda: {
            "sim": 0.30,
            "sigmatch": 0.20,
            "pass_rate": 0.15,
            "cachehit": 0.10,
            "coldstart": 0.10,
            "permrisk": 0.07,
            "depdepth": 0.08,
        }
    )
    create_gate: dict[str, float] = Field(
        default_factory=lambda: {
            "reuse_base": 0.55,
            "reuse_depth_bonus": 0.08,
            "generated_current_gain": 0.62,
            "explicit_expression_bonus": 0.10,
            "generated_future_gain": 0.25,
            "default_current_gain": 0.20,
            "default_future_gain": 0.05,
            "build_cost_base": 0.38,
            "build_cost_per_extra_arg": 0.04,
            "exec_cost": 0.18,
            "safety_cost": 0.06,
        }
    )
    dispatch_latency: dict[str, float] = Field(default_factory=lambda: {"base": 12.0, "per_arg": 6.0})


class StopPolicyProfile(BaseModel):
    require_verified_terminal: bool = True


class ControlProfile(BaseModel):
    model_order: list[str] = Field(default_factory=lambda: ["small", "medium", "large"])
    model_specs: dict[str, dict[str, float]] = Field(
        default_factory=lambda: {
            "small": {"solve": 0.60, "cost": 0.10, "latency": 0.10, "dollar": 0.10, "fail": 0.12},
            "medium": {"solve": 0.74, "cost": 0.20, "latency": 0.16, "dollar": 0.18, "fail": 0.08},
            "large": {"solve": 0.85, "cost": 0.35, "latency": 0.25, "dollar": 0.32, "fail": 0.04},
        }
    )
    assign_model_weights: dict[str, float] = Field(default_factory=lambda: {"cost": 0.20, "latency": 0.15, "dollar": 0.10, "fail": 0.20})
    required_solve: dict[str, float] = Field(default_factory=lambda: {"base": 0.60, "generated_bonus": 0.10, "dependency_bonus": 0.05})
    negative_steps_before_escalation: int = 2
    check_voi: dict[str, float] = Field(
        default_factory=lambda: {
            "local_positive": 0.30,
            "local_bias": -0.04,
            "subtree_positive": 0.22,
            "subtree_bias": -0.06,
            "repo_positive": 0.18,
            "repo_bias": -0.08,
            "benchmark_positive": 0.65,
            "benchmark_bias": -0.08,
        }
    )
    stop_policy: StopPolicyProfile = Field(default_factory=StopPolicyProfile)


class RuntimeProfile(BaseModel):
    prompts: PromptProfile = Field(default_factory=PromptProfile)
    runtime_provider: HostedProviderProfile = Field(default_factory=HostedProviderProfile, alias="provider")
    execution: ExecutionProfile = Field(default_factory=ExecutionProfile)
    evaluation: EvaluationProfile = Field(default_factory=EvaluationProfile)
    evolution: EvolutionProfile = Field(default_factory=EvolutionProfile)
    topology: TopologyProfile = Field(default_factory=TopologyProfile)
    memory: MemoryProfile = Field(default_factory=MemoryProfile)
    tooling: ToolingProfile = Field(default_factory=ToolingProfile)
    control: ControlProfile = Field(default_factory=ControlProfile)

    class Config:
        allow_population_by_field_name = True


def runtime_profile_path(runtime_dir: str | Path) -> Path:
    return Path(runtime_dir) / RUNTIME_PROFILE_FILE


def runtime_has_embedded_profile(runtime_dir: str | Path | None) -> bool:
    if runtime_dir is None:
        return False
    return runtime_profile_path(runtime_dir).exists()


def _default_profile_dict() -> dict[str, Any]:
    path = resources.files("agintor").joinpath("templates", "baseline_runtime", RUNTIME_PROFILE_FILE)
    return json.loads(path.read_text(encoding="utf-8"))


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def _normalize_legacy_profile_keys(payload: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(payload)
    legacy_provider = normalized.pop("provider", None)
    if isinstance(legacy_provider, dict):
        runtime_provider = normalized.get("runtime_provider", {})
        if not isinstance(runtime_provider, dict):
            runtime_provider = {}
        normalized["runtime_provider"] = _deep_merge(runtime_provider, legacy_provider)
    return normalized


def default_runtime_profile() -> RuntimeProfile:
    return model_validate(RuntimeProfile, _default_profile_dict())


def load_runtime_profile(
    runtime_dir: str | Path | None = None,
    *,
    profile_path: str | Path | None = None,
) -> RuntimeProfile:
    merged = _default_profile_dict()
    runtime_profile = runtime_profile_path(runtime_dir) if runtime_dir is not None else None
    if runtime_profile is not None and runtime_profile.exists():
        merged = _deep_merge(merged, json.loads(runtime_profile.read_text(encoding="utf-8")))
    if profile_path is not None:
        merged = _deep_merge(merged, json.loads(Path(profile_path).read_text(encoding="utf-8")))
    merged = _normalize_legacy_profile_keys(merged)
    return model_validate(RuntimeProfile, merged)


def resolve_runtime_profile(
    runtime_dir: str | Path | None = None,
    *,
    fallback_profile: RuntimeProfile | None = None,
    profile_path: str | Path | None = None,
) -> RuntimeProfile:
    if runtime_has_embedded_profile(runtime_dir) or profile_path is not None:
        return load_runtime_profile(runtime_dir, profile_path=profile_path)
    if fallback_profile is not None:
        return fallback_profile
    return default_runtime_profile()


def profile_to_json(profile: RuntimeProfile) -> str:
    return json.dumps(model_dump(profile), indent=2, sort_keys=True)
