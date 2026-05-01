from __future__ import annotations

from agintor.contracts import BenchmarkTask, CapabilityExchange
from agintor.core.versioning import RUNTIME_CONTRACT_VERSION


def _capability_exchange() -> CapabilityExchange:
    return CapabilityExchange(
        runtime_contract_version=RUNTIME_CONTRACT_VERSION,
        supported_backends=["local", "docker"],
        tool_runtimes=["python"],
        checkpoint_support=True,
        runtime_asset_capabilities={"traces": True, "checkpoints": True, "runtime_sdk": True},
        side_effect_receipts=True,
        resume_support=True,
        runtime_isolation_policy={"required_guarantees": []},
        supported_guarantees=[],
        effective_guarantees=[],
        required_env_names=[],
        required_env_any_of=[],
        capability_flags=["inspect", "run_batch", "benchmark_mode", "prompt_mode"],
    )


def _task(task_id: str) -> BenchmarkTask:
    return BenchmarkTask(
        task_id=task_id,
        family="top",
        prompt="Say hello.",
        task_type="structured_ops",
        operations=[],
        expected={},
    )
