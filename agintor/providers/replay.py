from __future__ import annotations

import json
import time
from pathlib import Path
from threading import Lock
from typing import Any, Mapping

from ..core.exceptions import AgintorError, ProviderConfigurationError, ProviderExhaustedError
from .base import (
    LocalDeterministicProvider,
    ModelProvider,
    load_api_key_from_file,
    load_openai_api_key_from_file,
    provider_kwargs_from_profile,
    resolve_api_key,
    resolve_openai_api_key,
)
from ..runtime.profile import HostedProviderProfile
from ..contracts import ModelRequest, ModelResponse, ReplayAllocation

from .payloads import _copy_json_like

class ReplayProvider(ModelProvider):
    """Offline replay provider for deterministic hosted-provider emulation."""

    class ReplayAllocator:
        def __init__(self, rows: list[dict[str, Any]], *, cursor: int = 0) -> None:
            self.rows = [dict(row) for row in rows]
            self.cursor = max(0, min(int(cursor or 0), len(self.rows)))
            self.lock = Lock()

    class ReplayCoordinator:
        def __init__(
            self,
            allocator: "ReplayProvider.ReplayAllocator",
            *,
            replay_file: str | None = None,
            cursor: int | None = None,
            cursor_start: int | None = None,
            cursor_end: int | None = None,
            use_allocator_cursor: bool = False,
        ) -> None:
            self._allocator = allocator
            self.replay_file = str(replay_file) if replay_file else None
            row_count = len(self._allocator.rows)
            default_start = self._allocator.cursor if use_allocator_cursor else 0
            self._cursor_start = max(
                0,
                min(int(cursor_start if cursor_start is not None else default_start), row_count),
            )
            raw_end = row_count if cursor_end is None else int(cursor_end)
            self._cursor_end = max(self._cursor_start, min(raw_end, row_count))
            if use_allocator_cursor:
                self._cursor = self._allocator.cursor
            else:
                seed_cursor = self._cursor_start if cursor is None else int(cursor)
                self._cursor = max(self._cursor_start, min(seed_cursor, self._cursor_end))
            self._use_allocator_cursor = bool(use_allocator_cursor)
            self._lock = Lock()

        def next_row(self) -> tuple[int, dict[str, Any]]:
            if self._use_allocator_cursor:
                with self._allocator.lock:
                    if self._allocator.cursor >= len(self._allocator.rows):
                        raise ProviderExhaustedError("Replay provider exhausted: no more recorded responses")
                    record_index = self._allocator.cursor
                    row = dict(self._allocator.rows[record_index])
                    self._allocator.cursor += 1
                    self._cursor = self._allocator.cursor
                    return record_index, row
            with self._lock:
                if self._cursor >= self._cursor_end:
                    raise ProviderExhaustedError("Replay provider exhausted: no more reserved responses")
                record_index = self._cursor
                row = dict(self._allocator.rows[record_index])
                self._cursor += 1
                return record_index, row

        def reserve_window(self, *, row_count: int, allocation_key: str) -> ReplayAllocation:
            size = max(0, int(row_count or 0))
            with self._allocator.lock:
                start = int(self._allocator.cursor)
                end = start + size
                if end > len(self._allocator.rows):
                    raise ProviderExhaustedError(
                        f"Replay provider exhausted while reserving {size} rows for {allocation_key}"
                    )
                self._allocator.cursor = end
                if self._use_allocator_cursor:
                    self._cursor = self._allocator.cursor
            return ReplayAllocation(
                allocation_key=str(allocation_key),
                cursor_start=start,
                cursor_end=end,
                next_cursor=start,
            )

        def clone_shared(self) -> "ReplayProvider.ReplayCoordinator":
            return ReplayProvider.ReplayCoordinator(
                self._allocator,
                replay_file=self.replay_file,
                use_allocator_cursor=True,
            )

        def clone_window(self, allocation: ReplayAllocation) -> "ReplayProvider.ReplayCoordinator":
            return ReplayProvider.ReplayCoordinator(
                self._allocator,
                replay_file=self.replay_file,
                cursor=allocation.next_cursor,
                cursor_start=allocation.cursor_start,
                cursor_end=allocation.cursor_end,
                use_allocator_cursor=False,
            )

        def current_allocation(self, allocation_key: str | None = None) -> ReplayAllocation | None:
            if self._use_allocator_cursor:
                return None
            return ReplayAllocation(
                allocation_key=str(allocation_key or ""),
                cursor_start=self._cursor_start,
                cursor_end=self._cursor_end,
                next_cursor=self._cursor,
            )

        def can_apply_allocation(self, allocation: ReplayAllocation | None) -> bool:
            if allocation is None:
                return False
            return int(allocation.cursor_end) <= len(self._allocator.rows)

        def snapshot_payload(self) -> dict[str, Any]:
            payload = {"kind": "replay", "replay_file": self.replay_file}
            if self.replay_file is None:
                payload["rows"] = _copy_json_like(self._allocator.rows)
            cursor_value = self._allocator.cursor if self._use_allocator_cursor else self._cursor
            if cursor_value:
                payload["cursor"] = int(cursor_value)
            if not self._use_allocator_cursor:
                payload["cursor_start"] = int(self._cursor_start)
                payload["cursor_end"] = int(self._cursor_end)
            return payload

    def __init__(
        self,
        rows: list[dict[str, Any]],
        *,
        replay_file: str | None = None,
        coordinator: "ReplayProvider.ReplayCoordinator" | None = None,
        cursor: int = 0,
        cursor_start: int | None = None,
        cursor_end: int | None = None,
    ) -> None:
        super().__init__("replay")
        use_allocator_cursor = cursor_start is None and cursor_end is None
        self._coordinator = coordinator or self.ReplayCoordinator(
            self.ReplayAllocator(rows, cursor=cursor),
            replay_file=replay_file,
            cursor=cursor,
            cursor_start=cursor_start,
            cursor_end=cursor_end,
            use_allocator_cursor=use_allocator_cursor,
        )
        self.replay_file = self._coordinator.replay_file

    @classmethod
    def from_file(
        cls,
        path: str | Path,
        *,
        cursor: int = 0,
        cursor_start: int | None = None,
        cursor_end: int | None = None,
    ) -> "ReplayProvider":
        replay_path = str(Path(path))
        try:
            payload = json.loads(Path(replay_path).read_text(encoding="utf-8"))
        except OSError as exc:
            raise ProviderConfigurationError(f"Replay provider file is not readable: {replay_path}") from exc
        if not isinstance(payload, list):
            raise ProviderConfigurationError("Replay provider file must contain a JSON array of response rows")
        if any(not isinstance(row, Mapping) for row in payload):
            raise ProviderConfigurationError("Replay provider rows must be JSON objects")
        return cls(
            [dict(row) for row in payload],
            replay_file=replay_path,
            cursor=cursor,
            cursor_start=cursor_start,
            cursor_end=cursor_end,
        )

    def _spawn_clone(self, coordinator: "ReplayProvider.ReplayCoordinator") -> "ReplayProvider":
        return ReplayProvider([], replay_file=self.replay_file, coordinator=coordinator)

    def shared_clone(self) -> "ReplayProvider":
        return self._spawn_clone(self._coordinator.clone_shared())

    def reserve_rows(self, row_count: int, *, allocation_key: str) -> ReplayAllocation:
        return self._coordinator.reserve_window(row_count=row_count, allocation_key=allocation_key)

    def clone_for_allocation(self, allocation: ReplayAllocation) -> "ReplayProvider":
        return self._spawn_clone(self._coordinator.clone_window(allocation))

    def current_allocation(self) -> ReplayAllocation | None:
        current = self._coordinator.current_allocation()
        if current is None:
            return None
        allocation_key = current.allocation_key or ""
        return current.model_copy(update={"allocation_key": allocation_key}, deep=True)

    def can_apply_allocation(self, allocation: ReplayAllocation | None) -> bool:
        return self._coordinator.can_apply_allocation(allocation)

    def generate(self, request: ModelRequest) -> ModelResponse:
        record_index, row = self._coordinator.next_row()
        metadata = getattr(request, "metadata", {}) or {}
        response = ModelResponse(
            text=str(row.get("text", "")),
            raw={
                "provider": "replay",
                "record_index": record_index,
                "request_model_class": request.model_class,
                "trace_context": dict(metadata.get("trace_context", {})) if isinstance(metadata, Mapping) else {},
            },
            model_name=str(row.get("model_name", f"replay/{request.model_class}")),
            input_tokens=int(row.get("input_tokens", 0) or 0),
            output_tokens=int(row.get("output_tokens", 0) or 0),
            token_estimate=int(row.get("token_estimate", 0) or 0),
            latency_s=float(row.get("latency_s", 0.0) or 0.0),
            dollar_cost=float(row.get("dollar_cost", 0.0) or 0.0),
            trace_call_id=str(row.get("trace_call_id") or row.get("call_id") or "").strip() or None,
        )
        self._record_usage(response)
        return response
