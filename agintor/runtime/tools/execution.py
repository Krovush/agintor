from __future__ import annotations

import ast
import contextlib
import io
import json
import math
import os
import shutil
import statistics
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional

from ...storage.artifacts import ArtifactPolicy
from ...core.exceptions import SafetyViolation, ValidationError
from ...contracts import (
    AsyncHandle,
    TaskLocalToolRegistrySnapshot,
    TaskLocalToolSnapshot,
    ToolExecutionResult,
    ToolSpec,
)
from ...utils import ensure_directory, file_digest, now_ts, stable_hash

from .models import (
    RegisteredTool,
    _AsyncProcessRecord,
)
from .registry import (
    ToolRegistry,
    _tool_filename,
)

def _async_artifact_stem(tool_name: str, handle_id: str) -> str:
    slug = tool_name.replace("/", "_").strip("_") or "tool"
    return f"{slug[:12]}_{handle_id[:8]}"


class ToolExecutionMixin:
    def run_tool(self, tool_name: str, args: Mapping[str, Any], task_id: str) -> ToolExecutionResult:
        start = time.perf_counter()
        tool = self.registry.get(tool_name)
        tool.historical_runs += 1
        tool.distinct_tasks.add(task_id)
        try:
            if tool.executor is not None:
                output = tool.executor(**args)
            else:
                output = self._run_python_tool(tool, args)
            tool.historical_passes += 1
            return ToolExecutionResult(tool_name=tool_name, output=output, latency_s=time.perf_counter() - start, success=True)
        except Exception as exc:
            return ToolExecutionResult(tool_name=tool_name, output=None, stderr=str(exc), latency_s=time.perf_counter() - start, success=False)

    def _run_python_tool(self, tool: RegisteredTool, args: Mapping[str, Any]) -> Any:
        sandbox_dir = self.sandbox_manager.ensure_environment(tool.spec)
        tool_file = sandbox_dir / _tool_filename(tool.spec)
        if not tool_file.exists():
            raise FileNotFoundError(f"tool source missing for {tool.spec.name}")
        namespace: Dict[str, Any] = {}
        exec(tool_file.read_text(encoding="utf-8"), namespace, namespace)
        if "run" not in namespace:
            raise ValidationError("generated tool source missing run()")
        return namespace["run"](**dict(args))

    def _artifact_paths(self, workspace: Path, artifact_stem: str) -> tuple[Path | None, Path | None, Path | None]:
        if not self.persist_artifacts:
            return None, None, None
        ensure_directory(workspace)
        return (
            workspace / f"{artifact_stem}.stdout",
            workspace / f"{artifact_stem}.stderr",
            workspace / f"{artifact_stem}.result.json",
        )

    def _write_async_artifacts(
        self,
        stdout_path: Path | None,
        stderr_path: Path | None,
        result_path: Path | None,
        *,
        stdout: str,
        stderr: str,
        output: Any,
    ) -> list[str]:
        refs: list[str] = []
        if stdout_path is not None:
            stdout_path.write_text(stdout, encoding="utf-8")
            refs.append(str(stdout_path))
        if stderr_path is not None:
            stderr_path.write_text(stderr, encoding="utf-8")
            refs.append(str(stderr_path))
        if result_path is not None and output is not None:
            result_path.write_text(json.dumps(output), encoding="utf-8")
            refs.append(str(result_path))
        return refs

    def launch_async(self, tool_name: str, args: Mapping[str, Any], workspace: Path, task_id: str) -> AsyncHandle:
        tool = self.registry.get(tool_name)
        tool.historical_runs += 1
        tool.distinct_tasks.add(task_id)
        sandbox_dir = self.sandbox_manager.ensure_environment(tool.spec)
        tool_file = sandbox_dir / _tool_filename(tool.spec)
        handle_workspace = Path(workspace)
        working_directory = handle_workspace if self.persist_artifacts else tool_file.parent
        if self.persist_artifacts:
            ensure_directory(handle_workspace)
        self._async_launch_counter += 1
        handle_id = stable_hash(tool_name, args, self._async_launch_counter)[:16]
        artifact_stem = _async_artifact_stem(tool_name, handle_id)
        stdout_path, stderr_path, result_path = self._artifact_paths(handle_workspace, artifact_stem)
        process_pid: int | None = None
        if not tool_file.exists():
            if tool.executor is not None:
                raise ValidationError(
                    "executor-backed async tools must be materialized into source before background execution"
                )
            raise FileNotFoundError(f"tool source missing for {tool.spec.name}")
        payload = json.dumps(dict(args), sort_keys=True)
        program = textwrap.dedent(
            f"""
            import json
            namespace = {{}}
            exec(open({repr(str(tool_file))}, 'r', encoding='utf-8').read(), namespace, namespace)
            output = namespace['run'](**json.loads({payload!r}))
            print(json.dumps(output))
            """
        )
        process = subprocess.Popen(
            [sys.executable, "-c", program],
            cwd=str(tool_file.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        process_pid = process.pid
        self._async_processes[handle_id] = _AsyncProcessRecord(
            process=process,
            state={"stdout": "", "stderr": "", "output": None, "artifact_refs": []},
        )
        handle = AsyncHandle(
            handle_id=handle_id,
            tool_name=tool_name,
            sandbox_hash=tool.sandbox_hash or self.sandbox_manager.sandbox_hash(tool.spec),
            working_directory=str(working_directory),
            launch_time=now_ts(),
            timeout=tool.spec.timeout_s,
            stdout_path=str(stdout_path) if stdout_path is not None else None,
            stderr_path=str(stderr_path) if stderr_path is not None else None,
            state="running",
            artifact_refs=[path for path in [str(result_path) if result_path is not None else None] if path],
            process_pid=process_pid,
        )
        return handle

    def wait_async(self, handle: AsyncHandle, poll_interval_s: float = 0.01) -> ToolExecutionResult:
        start = time.perf_counter()
        record = self._async_processes.get(handle.handle_id)
        if record is None:
            return ToolExecutionResult(
                tool_name=handle.tool_name,
                output=None,
                stderr="async process handle missing",
                latency_s=time.perf_counter() - start,
                success=False,
                async_handle_id=handle.handle_id,
            )
        process = record.process
        return_code = 0
        try:
            try:
                stdout, stderr = process.communicate(timeout=handle.timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
                return ToolExecutionResult(
                    tool_name=handle.tool_name,
                    output=None,
                    stdout=stdout or "",
                    stderr=(stderr or f"async tool timed out after {handle.timeout}s").strip(),
                    latency_s=time.perf_counter() - start,
                    success=False,
                    async_handle_id=handle.handle_id,
                )
        finally:
            self._async_processes.pop(handle.handle_id, None)
        record.state["stdout"] = stdout or ""
        record.state["stderr"] = stderr or ""
        return_code = process.returncode
        stdout = str(record.state.get("stdout", ""))
        stderr = str(record.state.get("stderr", ""))
        output = record.state.get("output")
        success = False
        try:
            output = json.loads(stdout or "null")
            success = return_code == 0 and not bool(stderr.strip())
            if return_code != 0 and not stderr.strip():
                stderr = f"process exited with code {return_code}"
                success = False
        except Exception as exc:
            output = None
            stderr = f"{stderr}\n{exc}".strip()
            success = False
        record.state["output"] = output
        record.state["artifact_refs"] = self._write_async_artifacts(
            Path(handle.stdout_path) if handle.stdout_path else None,
            Path(handle.stderr_path) if handle.stderr_path else None,
            Path(handle.artifact_refs[0]) if handle.artifact_refs else None,
            stdout=stdout,
            stderr=stderr,
            output=output,
        )
        if return_code not in (None, 0):
            message = f"process exited with code {return_code}"
            if message not in stderr:
                stderr = f"{stderr}\n{message}".strip()
            success = False
        if success:
            self.registry.get(handle.tool_name).historical_passes += 1
        return ToolExecutionResult(
            tool_name=handle.tool_name,
            output=output,
            stdout=stdout,
            stderr=stderr,
            latency_s=time.perf_counter() - start,
            success=success,
            async_handle_id=handle.handle_id,
        )

    def cancel_async_handle(self, handle_id: str, handle_table: Any) -> dict[str, Any]:
        handle = handle_table.get(handle_id)
        record = self._async_processes.pop(handle_id, None)
        if record is None:
            raise RuntimeError(f"async process handle {handle_id!r} is not tracked for cancellation")
        process = record.process
        stdout = ""
        stderr = ""
        if process.poll() is None:
            try:
                process.terminate()
                stdout, stderr = process.communicate(timeout=0.25)
            except subprocess.TimeoutExpired:
                process.kill()
                stdout, stderr = process.communicate()
        else:
            stdout, stderr = process.communicate()
        stderr = (stderr or "").strip()
        if process.returncode not in (None, 0):
            message = f"process terminated with code {process.returncode}"
            if message not in stderr:
                stderr = f"{stderr}\n{message}".strip()
        artifact_refs = self._write_async_artifacts(
            Path(handle.stdout_path) if handle.stdout_path else None,
            Path(handle.stderr_path) if handle.stderr_path else None,
            Path(handle.artifact_refs[0]) if handle.artifact_refs else None,
            stdout=stdout or "",
            stderr=stderr,
            output=None,
        )
        handle_table.update_state(handle_id, "cancelled")
        cancelled_handle = handle_table.get(handle_id)
        cancelled_handle.artifact_refs = artifact_refs or list(cancelled_handle.artifact_refs)
        handle_table.handles[handle_id] = cancelled_handle
        return {
            "handle_id": handle_id,
            "state": "cancelled",
            "stdout": stdout or "",
            "stderr": stderr,
            "artifact_refs": artifact_refs,
        }

    @staticmethod
    def _pid_is_running(process_pid: int | None) -> bool:
        if process_pid is None:
            return False
        try:
            pid = int(process_pid)
        except Exception:
            return False
        if pid <= 0:
            return False
        if os.name == "nt":
            completed = subprocess.run(
                ["tasklist", "/FI", f"PID eq {pid}"],
                capture_output=True,
                text=True,
                check=False,
            )
            return completed.returncode == 0 and str(pid) in str(completed.stdout or "")
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        return True

    def reconcile_async_handle(self, handle: AsyncHandle, handle_table: Any) -> dict[str, Any]:
        record = self._async_processes.get(handle.handle_id)
        if record is not None:
            finished = self.await_handle(handle.handle_id, handle_table)
            return {
                "status": str(finished.get("state", "") or "").strip() or "unresolved",
                "handle_id": handle.handle_id,
                "output": finished.get("output"),
                "stderr": finished.get("stderr"),
                "reconciliation_source": "live_process",
            }
        if handle.state in {"completed", "failed", "cancelled"}:
            output = None
            result_path = Path(handle.artifact_refs[0]) if handle.artifact_refs else None
            if result_path is not None and result_path.exists():
                try:
                    output = json.loads(result_path.read_text(encoding="utf-8"))
                except Exception:
                    output = None
            return {
                "status": handle.state,
                "handle_id": handle.handle_id,
                "output": output,
                "stderr": "",
                "reconciliation_source": "terminal_handle_state",
            }
        if handle.state != "running":
            return {
                "status": "unresolved",
                "handle_id": handle.handle_id,
                "reason": f"unsupported_handle_state:{handle.state}",
                "reconciliation_source": "durable_state",
            }
        result_path = Path(handle.artifact_refs[0]) if handle.artifact_refs else None
        if result_path is not None and result_path.exists():
            try:
                output = json.loads(result_path.read_text(encoding="utf-8"))
            except Exception:
                output = None
            handle_table.update_state(handle.handle_id, "completed")
            return {
                "status": "completed",
                "handle_id": handle.handle_id,
                "output": output,
                "stderr": "",
                "reconciliation_source": "artifact_result",
            }
        live_process = self._pid_is_running(handle.process_pid)
        return {
            "status": "unresolved",
            "handle_id": handle.handle_id,
            "reason": "live_process_without_runtime_record" if live_process else "missing_runtime_record",
            "live_process": live_process,
            "reconciliation_source": "durable_state",
        }

    def await_handle(self, handle_id: str, handle_table: Any) -> dict[str, Any]:
        handle = handle_table.get(handle_id)
        result = self.wait_async(handle)
        handle_table.update_state(handle_id, "completed" if result.success else "failed")
        return {
            "handle_id": handle_id,
            "state": "completed" if result.success else "failed",
            "latency_s": result.latency_s,
            "output": result.output,
            "stderr": result.stderr,
        }
