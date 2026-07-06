from __future__ import annotations

from .tracing import *  # noqa: F401,F403
from .providers import *  # noqa: F401,F403
from .factory import *  # noqa: F401,F403
from .execution import *  # noqa: F401,F403
from .state import *  # noqa: F401,F403
from .sessions import *  # noqa: F401,F403
from .runtime import *  # noqa: F401,F403
from .runtime_spec import *  # noqa: F401,F403
from .spec_actions import *  # noqa: F401,F403
from .branches import *  # noqa: F401,F403
from .side_effects import *  # noqa: F401,F403
from .checkpoints import *  # noqa: F401,F403
from .benchmarks import *  # noqa: F401,F403
from .protocol import *  # noqa: F401,F403
from .verifiers import *  # noqa: F401,F403
from .evidence import *  # noqa: F401,F403
from .validation import *  # noqa: F401,F403
from .oracle import *  # noqa: F401,F403
from .search import *  # noqa: F401,F403
from .runtime_spec import ToolSpec as RuntimeToolSpec  # noqa: F401
from .state import ToolSpec as ToolSpec  # noqa: F401

_FORWARD_REF_NAMESPACE = dict(globals())
for _model in (
    RuntimeStateSnapshot,
    BranchResumeSnapshot,
    BranchResult,
    CheckpointEnvelope,
    SuiteEvaluation,
    DomainEvidenceContract,
    ChallengeInstance,
    EvidenceRecord,
    ValidationClaim,
    ProofObligation,
    ValidatorHealth,
    ValidatorReport,
    ClaimPosterior,
    EvidenceLedger,
    ComparisonRecord,
    ArchitectureSignal,
    AlphaBudget,
    ValidationPlan,
    PairedComparison,
    ProgressSignal,
    PromotionDecision,
    RuntimeSpec,
    SpecAction,
    OraclePackage,
):
    if hasattr(_model, "model_rebuild"):
        _model.model_rebuild(_types_namespace=_FORWARD_REF_NAMESPACE)
    else:
        _model.update_forward_refs(**_FORWARD_REF_NAMESPACE)

del _model, _FORWARD_REF_NAMESPACE
