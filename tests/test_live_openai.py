from __future__ import annotations

import os

import pytest

from agintor.providers import OpenAIProvider
from agintor.schemas import ModelRequest


@pytest.mark.live_openai
def test_openai_provider_live_roundtrip_with_mock_credentials() -> None:
    provider = OpenAIProvider(api_key=os.environ.get("OPENAI_API_KEY", "sk-mock"))
    response = provider.generate(
        ModelRequest(
            instructions="Respond with the word pong.",
            prompt="ping",
            model_class=os.environ.get("AGINTOR_OPENAI_SMALL_MODEL", "gpt-5-mini"),
            seed=0,
            metadata={"mode": "text"},
        )
    )
    assert response.text.strip().lower() == "pong"
