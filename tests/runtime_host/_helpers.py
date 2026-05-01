from __future__ import annotations

from agintor.runtime.profile import load_runtime_profile
from agintor.core.versioning import RUNTIME_CONTRACT_VERSION
from agintor.contracts import (
    BenchmarkTask,
    CapabilityExchange,
    OperationSpec,
    RuntimeBatchResponse,
    RuntimeIsolationPolicy,
    RuntimeSolveResponse,
    SolveResult,
)


def _capability_exchange() -> CapabilityExchange:
    provider_profile = _runtime_profile().runtime_provider
    credential_group = [
        name
        for name in [
            str(provider_profile.api_key_env or "").strip(),
            str(provider_profile.api_key_file_env or "").strip(),
        ]
        if name
    ]
    return CapabilityExchange(
        runtime_contract_version=RUNTIME_CONTRACT_VERSION,
        supported_backends=["local", "docker"],
        tool_runtimes=["python"],
        checkpoint_support=True,
        runtime_asset_capabilities={"traces": True, "checkpoints": True, "runtime_sdk": True},
        side_effect_receipts=True,
        resume_support=True,
        runtime_isolation_policy=RuntimeIsolationPolicy(required_guarantees=[]),
        supported_guarantees=[
            "timeout_enforcement",
            "workspace_isolation",
            "environment_filtering",
            "process_cleanup",
            "network_disablement",
        ],
        effective_guarantees=[
            "timeout_enforcement",
            "workspace_isolation",
            "environment_filtering",
        ],
        required_env_names=[],
        required_env_any_of=[credential_group] if credential_group else [],
        capability_flags=["inspect", "run_batch", "benchmark_mode", "prompt_mode"],
    )

def _runtime_profile():
    return load_runtime_profile()

def _clear_runtime_provider_env(monkeypatch) -> None:
    monkeypatch.delenv("AGINTOR_MAS_MINIMAX_API_KEY", raising=False)
    monkeypatch.delenv("AGINTOR_MAS_MINIMAX_KEY_FILE", raising=False)

def _make_repo_patch_task(task_id: str) -> BenchmarkTask:
    return BenchmarkTask(
        task_id=task_id,
        family="tool",
        prompt="Update README.md.",
        task_type="bounded_repo_patch",
        file_paths=["README.md"],
        allowed_tool_categories=["filesystem/read", "filesystem/patch"],
        context_items=[],
        operations=[
            OperationSpec(
                op_id="apply_patch",
                kind="repo_patch",
                output_key="patch_result",
                description="Update README.md.",
                args={
                    "request_id": f"{task_id}.request",
                    "output_schema": {},
                    "target_file_paths": ["README.md"],
                },
                externally_visible=True,
            )
        ],
        expected=None,
        verifier_type="none",
        externally_visible=True,
        verification_required=False,
        allow_best_effort=True,
    )

class _FakeDockerExecutor:
    def __init__(
        self,
        *,
        capability_exchange: CapabilityExchange,
        solve_response: RuntimeSolveResponse | None = None,
        batch_response: RuntimeBatchResponse | None = None,
        resume_response: RuntimeSolveResponse | None = None,
        batch_handler=None,
    ) -> None:
        self.capability_exchange = capability_exchange
        self.solve_response = solve_response
        self.batch_response = batch_response
        self.resume_response = resume_response
        self.batch_handler = batch_handler
        self.inspect_requests: list[tuple[object, object]] = []
        self.solve_requests: list[tuple[object, object]] = []
        self.batch_requests: list[tuple[object, object]] = []
        self.resume_requests: list[tuple[object, object]] = []

    def inspect(self, runtime_dir, request):
        self.inspect_requests.append((runtime_dir, request))
        return self.capability_exchange

    def solve_protocol(self, runtime_dir, request, **kwargs):
        self.solve_requests.append((runtime_dir, request))
        assert self.solve_response is not None
        return self.solve_response

    def run_batch_protocol(self, runtime_dir, request, **kwargs):
        self.batch_requests.append((runtime_dir, request))
        if self.batch_handler is not None:
            return self.batch_handler(runtime_dir, request)
        assert self.batch_response is not None
        return self.batch_response

    def resume_protocol(self, runtime_dir, request, **kwargs):
        self.resume_requests.append((runtime_dir, request))
        assert self.resume_response is not None
        return self.resume_response

def _solve_response(
    *,
    request_id: str,
    capability_exchange: CapabilityExchange,
    artifact,
    mode: str = "user_request",
) -> RuntimeSolveResponse:
    return RuntimeSolveResponse(
        request_id=request_id,
        capability_exchange=capability_exchange,
        solve_result=SolveResult(
            request_id=request_id,
            runtime_hash="hash",
            mode=mode,
            artifact=artifact,
            status="best_effort",
            verification_status="best_effort",
            summary="ok",
            checks=[],
            budget={},
            provider_usage={},
            faults={"hard_invalid": False},
            verified=False,
            best_effort=True,
        ),
    )
