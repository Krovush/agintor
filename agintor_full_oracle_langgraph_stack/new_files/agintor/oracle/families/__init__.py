from __future__ import annotations

from .exact_private_answer import family as exact_private_answer_family
from .schema_artifact import family as schema_artifact_family
from .repo_patch import family as repo_patch_family
from .stateful_service import family as stateful_service_family
from .trace_state import family as trace_state_family
from .factual_grounded import family as factual_grounded_family
from .pairwise_preference import family as pairwise_preference_family
from .trading_outcome import family as trading_outcome_family
from .human_audit import family as human_audit_family
from .inspect_runner import family as inspect_runner_family
from .openai_eval_runner import family as openai_eval_runner_family
from .consent_proof import family as consent_proof_family


def all_default_families():
    return [
        exact_private_answer_family(),
        schema_artifact_family(),
        repo_patch_family(),
        stateful_service_family(),
        trace_state_family(),
        factual_grounded_family(),
        pairwise_preference_family(),
        trading_outcome_family(),
        human_audit_family(),
        inspect_runner_family(),
        openai_eval_runner_family(),
        consent_proof_family(),
    ]


__all__ = ["all_default_families"]
