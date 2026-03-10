from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from .pydantic_compat import model_dump, model_validate
from .schemas import BenchmarkTask, RunResult
from .utils import ensure_directory, file_digest, stable_hash


class DockerRuntimeExecutor:
    def __init__(
        self,
        workspace: Path,
        repo_root: Path | None = None,
        image_name_prefix: str = "agintor-runtime",
        base_image: str = "python:3.11-slim",
    ) -> None:
        self.workspace = ensure_directory(workspace)
        self.repo_root = repo_root or Path(__file__).resolve().parent.parent
        self.image_name_prefix = image_name_prefix
        self.base_image = base_image
        self.image_tag = f"{self.image_name_prefix}:{self._source_digest()[:12]}"

    def _source_digest(self) -> str:
        relevant = [self.repo_root / "pyproject.toml", self.repo_root / "README.md"]
        relevant.extend(sorted((self.repo_root / "agintor").rglob("*.py")))
        relevant.extend(sorted((self.repo_root / "agintor").rglob("*.json")))
        parts = []
        for path in relevant:
            if path.exists():
                parts.append(f"{path.relative_to(self.repo_root)}::{file_digest(path)}")
        return stable_hash(*parts)

    def _dockerfile_text(self) -> str:
        return "\n".join(
            [
                f"FROM {self.base_image}",
                "WORKDIR /opt/agintor",
                "COPY pyproject.toml README.md /opt/agintor/",
                "COPY agintor /opt/agintor/agintor",
                "RUN pip install --no-cache-dir '.[openai]'",
                'ENTRYPOINT ["python", "-m", "agintor.container_entry"]',
            ]
        )

    def ensure_image(self) -> None:
        inspect = subprocess.run(
            ["docker", "image", "inspect", self.image_tag],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if inspect.returncode == 0:
            return
        dockerfile_dir = ensure_directory(self.workspace / "docker")
        dockerfile_path = dockerfile_dir / f"Dockerfile.{self._source_digest()[:12]}"
        if not dockerfile_path.exists():
            dockerfile_path.write_text(self._dockerfile_text(), encoding="utf-8")
        completed = subprocess.run(
            ["docker", "build", "-f", str(dockerfile_path), "-t", self.image_tag, str(self.repo_root)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "docker build failed")

    def run_task(
        self,
        runtime_dir: str | Path,
        task: BenchmarkTask,
        seed: int,
        *,
        provider_name: str,
        api_key_file: str | Path | None = None,
    ) -> RunResult:
        return self.run_unit(
            runtime_dir,
            [task],
            seed,
            provider_name=provider_name,
            api_key_file=api_key_file,
        )[0]

    def run_unit(
        self,
        runtime_dir: str | Path,
        tasks: list[BenchmarkTask],
        seed: int,
        *,
        provider_name: str,
        api_key_file: str | Path | None = None,
    ) -> list[RunResult]:
        self.ensure_image()
        runtime_path = Path(runtime_dir).resolve()
        task_ids = [task.task_id for task in tasks]
        run_dir = ensure_directory(self.workspace / stable_hash(runtime_path, task_ids, seed)[:12])
        task_json = run_dir / "tasks.json"
        output_json = run_dir / "run_result.json"
        workspace_dir = ensure_directory(run_dir / "workspace")
        task_json.write_text(json.dumps([model_dump(task) for task in tasks], indent=2, sort_keys=True), encoding="utf-8")
        mounts = [
            f"{runtime_path}:/mnt/runtime:ro",
            f"{task_json.resolve()}:/mnt/tasks.json:ro",
            f"{workspace_dir.resolve()}:/mnt/workspace",
            f"{output_json.parent.resolve()}:/mnt/output",
        ]
        command = [
            "docker",
            "run",
            "--rm",
        ]
        for env_name in (
            "AGINTOR_OPENAI_SMALL_MODEL",
            "AGINTOR_OPENAI_MEDIUM_MODEL",
            "AGINTOR_OPENAI_LARGE_MODEL",
            "AGINTOR_OPENAI_PRICING",
            "OPENAI_BASE_URL",
        ):
            env_value = os.environ.get(env_name)
            if env_value:
                command.extend(["-e", f"{env_name}={env_value}"])
        for mount in mounts:
            command.extend(["-v", mount])
        container_key_path: str | None = None
        if api_key_file:
            host_key = Path(api_key_file).resolve()
            container_key_path = "/mnt/keys/openai_api_key.txt"
            command.extend(["-v", f"{host_key}:{container_key_path}:ro"])
        command.extend(
            [
                self.image_tag,
                "run-runtime-unit",
                "--runtime-dir",
                "/mnt/runtime",
                "--tasks-json",
                "/mnt/tasks.json",
                "--seed",
                str(seed),
                "--provider",
                provider_name,
                "--output-json",
                "/mnt/output/run_result.json",
                "--workspace",
                "/mnt/workspace",
            ]
        )
        if container_key_path:
            command.extend(["--api-key-file", container_key_path])
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "docker run failed")
        return [model_validate(RunResult, payload) for payload in json.loads(output_json.read_text(encoding="utf-8"))]
