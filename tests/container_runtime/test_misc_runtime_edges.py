from __future__ import annotations

import errno
import json
from pathlib import Path

from agintor.providers import LocalDeterministicProvider
from agintor.contracts import ModelRequest
from agintor.runtime.kernel.io import BoundedIOMixin


def test_bounded_write_falls_back_when_bind_mounted_file_cannot_be_replaced(tmp_path: Path, monkeypatch):
    target = tmp_path / "mounted-file.txt"
    target.write_text("old", encoding="utf-8")
    original_replace = Path.replace

    def busy_replace(self, target_path):
        if Path(target_path) == target:
            raise OSError(errno.EBUSY, "device or resource busy")
        return original_replace(self, target_path)

    monkeypatch.setattr(Path, "replace", busy_replace)

    BoundedIOMixin._write_text_atomic(target, "new")

    assert target.read_text(encoding="utf-8") == "new"


def test_local_provider_returns_json_for_repo_patch() -> None:
    provider = LocalDeterministicProvider()
    response = provider.generate(
        ModelRequest(
            instructions="Return JSON only with keys summary and files.",
            prompt='Update the file.\nTarget files:\n[{"path": "request file.txt", "content": "old"}]',
            model_class="small",
            seed=0,
            metadata={
                "mode": "repo_patch",
                "payload": {"target_file_paths": ["request file.txt"]},
            },
        )
    )

    payload = json.loads(response.text)

    assert payload["files"][0]["path"] == "request file.txt"
    assert payload["files"][0]["updated_content"] == "old\nLocal deterministic repo_patch update.\n"
