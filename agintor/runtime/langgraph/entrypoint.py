from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ...contracts import BenchmarkTask
from .adapters import run_spec_task


def solve(
    runtime_dir: str | Path,
    task_payload: dict[str, Any],
    *,
    request_id: str,
    seed: int = 0,
    runtime_hash: str = "",
    provider: Any | None = None,
    trace_context: Any | None = None,
) -> dict[str, Any]:
    task = BenchmarkTask.model_validate(task_payload)
    return run_spec_task(
        runtime_dir,
        task,
        request_id=request_id,
        seed=seed,
        runtime_hash=runtime_hash,
        provider=provider,
        trace_context=trace_context,
    )


def main() -> None:
    import sys
    payload = json.loads(sys.stdin.read() or "{}")
    result = solve(
        payload["runtime_dir"],
        payload["task"],
        request_id=payload.get("request_id", ""),
        seed=int(payload.get("seed", 0) or 0),
        runtime_hash=payload.get("runtime_hash", ""),
        trace_context=payload.get("trace_context"),
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()


__all__ = ["solve"]
