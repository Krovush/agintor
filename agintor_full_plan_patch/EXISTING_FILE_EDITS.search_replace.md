# Existing file edits as SEARCH/REPLACE diffs

Apply these after copying the files under `new_files/` into the repo root.

## agintor/contracts/__init__.py

### Add new contract exports

SEARCH:
```python
from .runtime import *  # noqa: F401,F403
from .branches import *  # noqa: F401,F403
```

REPLACE:
```python
from .runtime import *  # noqa: F401,F403
from .runtime_spec import *  # noqa: F401,F403
from .spec_actions import *  # noqa: F401,F403
from .branches import *  # noqa: F401,F403
```

### Export oracle contracts after evidence

SEARCH:
```python
from .evidence import *  # noqa: F401,F403
from .search import *  # noqa: F401,F403
```

REPLACE:
```python
from .evidence import *  # noqa: F401,F403
from .oracle import *  # noqa: F401,F403
from .search import *  # noqa: F401,F403
```

### Rebuild oracle forward refs

SEARCH:
```python
    ProgressSignal,
    PromotionDecision,
):
```

REPLACE:
```python
    ProgressSignal,
    PromotionDecision,
    RuntimeSpec,
    SpecAction,
    OraclePackage,
    OracleEvaluationSummary,
):
```

## agintor/contracts/runtime.py

### Add v2 runtime metadata to RuntimeManifest

SEARCH:
```python
class RuntimeManifest(BaseModel):
    runtime_id: str
    version: str
    policy_modules: Dict[str, str]
    mutable_files: List[str]
    immutable_manifest: List[str]
    metadata: Dict[str, Any] = Field(default_factory=dict)
```

REPLACE:
```python
class RuntimeManifest(BaseModel):
    runtime_id: str
    version: str
    policy_modules: Dict[str, str]
    mutable_files: List[str]
    immutable_manifest: List[str]
    runtime_kind: Literal["policy_modules_v1", "langgraph_spec_v2", "tradingagents_langgraph_v1"] = "policy_modules_v1"
    runtime_spec_path: str = ""
    runtime_spec_digest: str = ""
    oracle_package_hash: str = ""
    metadata: Dict[str, Any] = Field(default_factory=dict)
```

## agintor/contracts/evidence.py

### Thread oracle/runtime identity through EvidenceRecord

SEARCH:
```python
class EvidenceRecord(EvidenceModel):
    record_id: str
    contract_id: str
    challenge_id: str
    candidate_runtime_hash: str
    parent_runtime_hash: str = ""
```

REPLACE:
```python
class EvidenceRecord(EvidenceModel):
    record_id: str
    contract_id: str
    challenge_id: str
    candidate_runtime_hash: str
    parent_runtime_hash: str = ""
    oracle_package_hash: str = ""
    runtime_spec_digest: str = ""
```

### Thread oracle/runtime identity through PairedComparison

SEARCH:
```python
class PairedComparison(EvidenceModel):
    comparison_id: str
    parent_runtime_hash: str
    child_runtime_hash: str
    contract_id: str = ""
```

REPLACE:
```python
class PairedComparison(EvidenceModel):
    comparison_id: str
    parent_runtime_hash: str
    child_runtime_hash: str
    contract_id: str = ""
    oracle_package_hash: str = ""
    parent_runtime_spec_digest: str = ""
    child_runtime_spec_digest: str = ""
```

### Thread oracle/runtime identity through ProgressSignal

SEARCH:
```python
class ProgressSignal(EvidenceModel):
    signal_id: str = ""
    parent_runtime_hash: str
    child_runtime_hash: str
    contract_id: str = ""
```

REPLACE:
```python
class ProgressSignal(EvidenceModel):
    signal_id: str = ""
    parent_runtime_hash: str
    child_runtime_hash: str
    contract_id: str = ""
    oracle_package_hash: str = ""
    parent_runtime_spec_digest: str = ""
    child_runtime_spec_digest: str = ""
```

### Thread oracle/runtime identity through PromotionDecision

SEARCH:
```python
class PromotionDecision(EvidenceModel):
    decision_id: str
    decision_type: PromotionDecisionType | str
    contract_id: str = ""
```

REPLACE:
```python
class PromotionDecision(EvidenceModel):
    decision_id: str
    decision_type: PromotionDecisionType | str
    contract_id: str = ""
    oracle_package_hash: str = ""
    parent_runtime_spec_digest: str = ""
    child_runtime_spec_digest: str = ""
```

## agintor/evaluation/benchmarks.py

### Import OraclePackage

SEARCH:
```python
from ..contracts import BenchmarkTask, DomainEvidenceContract, OperationSpec
```

REPLACE:
```python
from ..contracts import BenchmarkTask, DomainEvidenceContract, OperationSpec, OraclePackage
```

### Add oracle_package to BenchmarkSuite

SEARCH:
```python
@dataclass(frozen=True)
class BenchmarkSuite:
    name: str
    train: list[BenchmarkTask]
    val: list[BenchmarkTask]
    test: list[BenchmarkTask]
    proxy: list[BenchmarkTask]
    evidence_contract: DomainEvidenceContract | None = None
```

REPLACE:
```python
@dataclass(frozen=True)
class BenchmarkSuite:
    name: str
    train: list[BenchmarkTask]
    val: list[BenchmarkTask]
    test: list[BenchmarkTask]
    proxy: list[BenchmarkTask]
    evidence_contract: DomainEvidenceContract | None = None
    oracle_package: OraclePackage | None = None
```

### Load oracle packages from suite JSON

SEARCH:
```python
        proxy=[(BenchmarkTask).model_validate(item) for item in data["proxy"]],
        evidence_contract=DomainEvidenceContract.model_validate(data["evidence_contract"]) if data.get("evidence_contract") else None,
    )
```

REPLACE:
```python
        proxy=[(BenchmarkTask).model_validate(item) for item in data["proxy"]],
        evidence_contract=DomainEvidenceContract.model_validate(data["evidence_contract"]) if data.get("evidence_contract") else None,
        oracle_package=OraclePackage.model_validate(data["oracle_package"]) if data.get("oracle_package") else None,
    )
```

## agintor/factory/planning.py

### Import OracleCompiler

SEARCH:
```python
from ..core.versioning import RUNTIME_CONTRACT_VERSION
```

REPLACE:
```python
from ..core.versioning import RUNTIME_CONTRACT_VERSION
from ..oracle.compiler import OracleCompiler
```

### Add helper to build frozen oracle package

SEARCH:
```python
def _build_verifier_bundle(plan: BenchmarkPlan, suite: BenchmarkSuite) -> VerifierBundle:
```

REPLACE:
```python
def _build_oracle_package(goal_spec: GoalSpec, suite: BenchmarkSuite, runtime_spec=None):
    """Create the frozen validation artifact for a factory build.

    This wraps current suite/evidence behavior into the new OraclePackage spine
    while allowing future adaptive compilation behind the same interface.
    """
    return OracleCompiler().compile(
        goal_spec,
        runtime_spec,
        task_sets=[],
    )


def _build_verifier_bundle(plan: BenchmarkPlan, suite: BenchmarkSuite) -> VerifierBundle:
```

## agintor/evaluation/evaluator.py

### Import OraclePackage and finalizer

SEARCH:
```python
from ..contracts import (
    BenchmarkTask,
    DomainEvidenceContract,
```

REPLACE:
```python
from ..contracts import (
    BenchmarkTask,
    DomainEvidenceContract,
    OraclePackage,
```

SEARCH:
```python
from ..utils import ensure_directory, stable_hash
```

REPLACE:
```python
from ..utils import ensure_directory, stable_hash
from ..oracle.package_io import finalize_oracle_package
```

### Add oracle_package constructor parameter

SEARCH:
```python
        trace_context: OpenAITraceContext | None = None,
        evidence_contract: DomainEvidenceContract | None = None,
    ) -> None:
```

REPLACE:
```python
        trace_context: OpenAITraceContext | None = None,
        evidence_contract: DomainEvidenceContract | None = None,
        oracle_package: OraclePackage | None = None,
    ) -> None:
```

### Resolve evidence contract from package first

SEARCH:
```python
        self.trace_context = trace_context
        self.evidence_contract = evidence_contract or getattr(suite, "evidence_contract", None)
```

REPLACE:
```python
        self.trace_context = trace_context
        self.oracle_package = finalize_oracle_package(oracle_package or getattr(suite, "oracle_package", None)) if (oracle_package or getattr(suite, "oracle_package", None)) is not None else None
        self.oracle_package_hash = self.oracle_package.package_hash if self.oracle_package is not None else ""
        self.evidence_contract = evidence_contract or (self.oracle_package.evidence_contract if self.oracle_package is not None else None) or getattr(suite, "evidence_contract", None)
```

### Add oracle identity to EvidenceRecord rows

SEARCH:
```python
                candidate_runtime_hash=evaluation.runtime_hash,
                parent_runtime_hash=decision.parent_runtime_hash,
```

REPLACE:
```python
                candidate_runtime_hash=evaluation.runtime_hash,
                parent_runtime_hash=decision.parent_runtime_hash,
                oracle_package_hash=self.oracle_package_hash,
```

### Add oracle identity to Stage 4 metrics

SEARCH:
```python
            "promotion_decision": decision.model_dump(mode="json", exclude_none=True),
            "progress_decision": decision_type,
```

REPLACE:
```python
            "promotion_decision": decision.model_dump(mode="json", exclude_none=True),
            "progress_decision": decision_type,
            "oracle_package_hash": self.oracle_package_hash,
```

## agintor/evaluation/progress_oracle.py

### Carry oracle identity from evidence contract metadata into comparisons

SEARCH:
```python
            contract_id=contract.contract_id if contract is not None else "implicit_suite_progress_contract",
            challenge_ids=challenge_ids,
```

REPLACE:
```python
            contract_id=contract.contract_id if contract is not None else "implicit_suite_progress_contract",
            oracle_package_hash=str((contract.artifact_refs or {}).get("oracle_package_hash", "")) if contract is not None and isinstance(contract.artifact_refs, dict) else "",
            challenge_ids=challenge_ids,
```

### Carry oracle identity into ProgressSignal

SEARCH:
```python
            child_runtime_hash=comparison.child_runtime_hash,
            contract_id=contract.contract_id if contract is not None else comparison.contract_id,
            decision_type=decision_type,
```

REPLACE:
```python
            child_runtime_hash=comparison.child_runtime_hash,
            contract_id=contract.contract_id if contract is not None else comparison.contract_id,
            oracle_package_hash=comparison.oracle_package_hash,
            parent_runtime_spec_digest=comparison.parent_runtime_spec_digest,
            child_runtime_spec_digest=comparison.child_runtime_spec_digest,
            decision_type=decision_type,
```

### Carry oracle identity into PromotionDecision

SEARCH:
```python
        return PromotionDecision(
            decision_id=stable_hash("promotion-decision", comparison.comparison_id, decision_type, reason_codes)[:24],
            decision_type=decision_type,
            contract_id=contract.contract_id if contract is not None else comparison.contract_id,
```

REPLACE:
```python
        return PromotionDecision(
            decision_id=stable_hash("promotion-decision", comparison.comparison_id, decision_type, reason_codes)[:24],
            decision_type=decision_type,
            contract_id=contract.contract_id if contract is not None else comparison.contract_id,
            oracle_package_hash=comparison.oracle_package_hash,
            parent_runtime_spec_digest=comparison.parent_runtime_spec_digest,
            child_runtime_spec_digest=comparison.child_runtime_spec_digest,
```

## agintor/runtime/loader.py

### Import RuntimeSpec digest support

SEARCH:
```python
from ..contracts import CapabilityExchange, DeploymentContract, KernelManifest, RuntimeIsolationPolicy, RuntimeManifest
```

REPLACE:
```python
from ..contracts import CapabilityExchange, DeploymentContract, KernelManifest, RuntimeIsolationPolicy, RuntimeManifest, RuntimeSpec, runtime_spec_digest
```

### Include runtime_spec.json in runtime identity

SEARCH:
```python
    for rel_path in manifest.immutable_manifest:
        if Path(rel_path).name == RUNTIME_PROFILE_FILE:
```

REPLACE:
```python
    runtime_spec_rel = str(getattr(manifest, "runtime_spec_path", "") or manifest.metadata.get("runtime_spec_path", "") if isinstance(manifest.metadata, dict) else "").strip()
    if runtime_spec_rel:
        spec_path = _resolve_manifest_path(runtime_path, runtime_spec_rel)
        runtime_spec = RuntimeSpec.model_validate(json.loads(spec_path.read_text(encoding="utf-8")))
        immutable_fingerprints[runtime_spec_rel] = runtime_spec_digest(runtime_spec)
    elif (runtime_path / "runtime_spec.json").exists():
        runtime_spec = RuntimeSpec.model_validate(json.loads((runtime_path / "runtime_spec.json").read_text(encoding="utf-8")))
        immutable_fingerprints["runtime_spec.json"] = runtime_spec_digest(runtime_spec)
    for rel_path in manifest.immutable_manifest:
        if Path(rel_path).name == RUNTIME_PROFILE_FILE:
```

## agintor/search/engine.py

### Import spec mutator objects

SEARCH:
```python
from ..search.mutators import HeuristicPatchMutator, MutationContext, ProviderPatchMutator
```

REPLACE:
```python
from ..search.mutators import HeuristicPatchMutator, MutationContext, ProviderPatchMutator
from ..search.spec_mutator import SpecActionMutator, SpecMutationContext
```

### Detect spec-backed runtimes in constructor

SEARCH:
```python
        self._baseline_manifest = self._load_runtime(self.baseline_runtime_dir).manifest
```

REPLACE:
```python
        self._baseline_manifest = self._load_runtime(self.baseline_runtime_dir).manifest
        self.spec_backed_runtime = str(getattr(self._baseline_manifest, "runtime_kind", "policy_modules_v1")) in {"langgraph_spec_v2", "tradingagents_langgraph_v1"} or (self.baseline_runtime_dir / "runtime_spec.json").exists()
        self.spec_mutator = SpecActionMutator(provider if normalized_mutator in {"provider", "openai"} else None, use_provider=normalized_mutator in {"provider", "openai"}) if self.spec_backed_runtime else None
```

## agintor/cli.py

### Import oracle helpers

SEARCH:
```python
from .runtime.profile import RUNTIME_PROFILE_FILE, RuntimeProfile, load_runtime_profile
```

REPLACE:
```python
from .runtime.profile import RUNTIME_PROFILE_FILE, RuntimeProfile, load_runtime_profile
from .oracle.package_io import load_oracle_package, write_oracle_package
from .oracle.qa import run_oracle_qa
from .oracle.projections import public_oracle_projection
from .oracle.compiler import OracleCompiler
from .contracts import GoalSpec, default_langgraph_runtime_spec
```

### Add oracle inspection commands before module entrypoint

SEARCH:
```python
if __name__ == "__main__":
    app()
```

REPLACE:
```python
@app.command("inspect-oracle")
def inspect_oracle_cmd(package_path: str, public: bool = typer.Option(False, "--public")) -> None:
    package = load_oracle_package(package_path)
    payload = public_oracle_projection(package) if public else package.model_dump(mode="json", exclude_none=True)
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command("oracle-qa")
def oracle_qa_cmd(package_path: str) -> None:
    package = load_oracle_package(package_path)
    report = run_oracle_qa(package)
    typer.echo(json.dumps(report.model_dump(mode="json"), indent=2, sort_keys=True))
    if not report.passed:
        raise typer.Exit(code=1)


@app.command("compile-oracle")
def compile_oracle_cmd(goal: str, destination: str) -> None:
    goal_spec = GoalSpec(
        goal_id=f"goal.{abs(hash(goal))}",
        raw_prompt=goal,
        normalized_goal=goal.strip(),
    )
    runtime_spec = default_langgraph_runtime_spec(runtime_id="runtime.preview", name="Runtime Preview")
    package = OracleCompiler().compile(goal_spec, runtime_spec)
    frozen = write_oracle_package(package, destination)
    typer.echo(json.dumps({"package_id": frozen.package_id, "package_hash": frozen.package_hash, "destination": destination}, indent=2, sort_keys=True))


if __name__ == "__main__":
    app()
```
