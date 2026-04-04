from __future__ import annotations

import json
from importlib import resources
from typing import Any

from pydantic import BaseModel, Field


class PromptSpec(BaseModel):
    prompt_id: str
    description: str = ""
    instructions: str
    model_class: str = "medium"
    max_output_tokens: int | None = None
    allowed_tools: list[str] = Field(default_factory=list)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    output_schema: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


def _resource_package() -> str:
    return (__package__ or "agintor").split(".", 1)[0]


def _prompt_path(prompt_id: str):
    return resources.files(_resource_package()).joinpath("templates", "prompts", f"{prompt_id}.json")


def load_prompt_spec(prompt_id: str) -> PromptSpec:
    path = _prompt_path(prompt_id)
    if not path.is_file():
        raise FileNotFoundError(f"unknown prompt id: {prompt_id}")
    return PromptSpec.parse_obj(json.loads(path.read_text(encoding="utf-8")))


def prompt_instructions(prompt_id: str) -> str:
    return load_prompt_spec(prompt_id).instructions
