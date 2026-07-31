"""Gemini OpenAI-compatible model adapter."""

from __future__ import annotations

import os

from agent.execution.errors import ModelInvocationError
from llm.aihubmix import AiHubMixModelClient
from llm.config import load_project_environment

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_GEMINI_BASE_URL = (
    "https://generativelanguage.googleapis.com/v1beta/openai/"
)


class GeminiModelClient(AiHubMixModelClient):
    def __init__(
        self,
        *,
        model_name: str = DEFAULT_GEMINI_MODEL,
        api_key: str | None = None,
        base_url: str | None = None,
        client=None,
    ) -> None:
        load_project_environment()
        key = api_key or os.getenv("GEMINI_API_KEY")
        if client is None and not key:
            raise ModelInvocationError(
                "configure_model",
                "GEMINI_API_KEY is required for the Gemini provider",
            )
        super().__init__(
            model_name=model_name,
            api_key=key or "test-client-key",
            base_url=base_url or DEFAULT_GEMINI_BASE_URL,
            client=client,
        )
