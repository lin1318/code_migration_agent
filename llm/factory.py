"""Environment-driven patch model selection."""

from __future__ import annotations

import os

from agent.execution.errors import ModelInvocationError
from llm.aihubmix import AiHubMixModelClient, DEFAULT_MODEL_NAME
from llm.gemini import DEFAULT_GEMINI_MODEL, GeminiModelClient
from llm.config import load_project_environment


def create_model_client(
    *,
    provider: str | None = None,
    model_name: str | None = None,
):
    load_project_environment()
    selected_provider = (
        provider or os.getenv("PATCH_MODEL_PROVIDER", "aihubmix")
    ).lower()
    selected_model = model_name or os.getenv(
        "PATCH_MODEL_NAME",
        DEFAULT_MODEL_NAME,
    )
    if selected_provider not in {"aihubmix", "gemini"}:
        raise ModelInvocationError(
            "configure_model",
            f"unsupported PATCH_MODEL_PROVIDER: {selected_provider}",
        )
    return AiHubMixModelClient(model_name=selected_model)


def create_bundle_model_client(
    *,
    provider: str | None = None,
    model_name: str | None = None,
):
    """Create the Stage 9 provider with Gemini defaults."""

    load_project_environment()
    selected_provider = (
        provider or os.getenv("PATCH_MODEL_PROVIDER", "gemini")
    ).lower()
    if selected_provider == "gemini":
        selected_model = model_name or os.getenv(
            "PATCH_MODEL_NAME",
            DEFAULT_GEMINI_MODEL,
        )
        return GeminiModelClient(model_name=selected_model)
    if selected_provider == "aihubmix":
        selected_model = model_name or os.getenv(
            "PATCH_MODEL_NAME",
            DEFAULT_MODEL_NAME,
        )
        return AiHubMixModelClient(model_name=selected_model)
    raise ModelInvocationError(
        "configure_model",
        f"unsupported PATCH_MODEL_PROVIDER: {selected_provider}",
    )


def create_file_bundle_model_client(
    *,
    provider: str | None = None,
    model_name: str | None = None,
    timeout_seconds: float | None = None,
):
    """Create the provider used for deterministic one-file bundles."""

    load_project_environment()
    selected_provider = (
        provider or os.getenv("PATCH_MODEL_PROVIDER", "gemini")
    ).lower()
    if selected_provider == "gemini":
        selected_model = model_name or os.getenv(
            "PATCH_MODEL_NAME",
            DEFAULT_GEMINI_MODEL,
        )
        return GeminiModelClient(
            model_name=selected_model,
            timeout_seconds=timeout_seconds,
        )
    if selected_provider == "aihubmix":
        selected_model = model_name or os.getenv(
            "PATCH_MODEL_NAME",
            DEFAULT_MODEL_NAME,
        )
        return AiHubMixModelClient(
            model_name=selected_model,
            timeout_seconds=timeout_seconds,
        )
    raise ModelInvocationError(
        "configure_model",
        f"unsupported PATCH_MODEL_PROVIDER: {selected_provider}",
    )
