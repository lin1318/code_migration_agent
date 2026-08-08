"""AiHubMix OpenAI-compatible adapter for Gemini patch generation."""

from __future__ import annotations

import os

from pydantic import ValidationError

from agent.execution.errors import ModelInvocationError
from agent.execution.models import MigrationTaskContext
from llm.config import load_project_environment
from models.bundle_execution import BundlePatchProposal, MigrationBundleContext
from models.file_bundle_execution import (
    FileBundleExecutionContext,
    FilePatchProposal,
)
from models.patch_proposal import PatchProposal

DEFAULT_BASE_URL = "https://aihubmix.com/v1"
DEFAULT_MODEL_NAME = "gemini-3.6-flash-free"


class AiHubMixModelClient:
    def __init__(
        self,
        *,
        model_name: str = DEFAULT_MODEL_NAME,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout_seconds: float | None = None,
        client=None,
    ) -> None:
        load_project_environment()
        key = api_key or os.getenv("AIHUBMIX_API_KEY")
        if client is None and not key:
            raise ModelInvocationError(
                "configure_model",
                "AIHUBMIX_API_KEY is required for the AiHubMix provider",
            )
        self._model_name = model_name
        self._base_url = base_url or os.getenv(
            "AIHUBMIX_BASE_URL",
            DEFAULT_BASE_URL,
        )
        if client is None:
            try:
                import openai
            except ImportError as error:
                raise ModelInvocationError(
                    "configure_model",
                    "the openai package is not installed",
                ) from error
            if timeout_seconds is None:
                client = openai.OpenAI(
                    api_key=key,
                    base_url=self._base_url,
                )
            else:
                client = openai.OpenAI(
                    api_key=key,
                    base_url=self._base_url,
                    timeout=timeout_seconds,
                )
        self._client = client

    @property
    def model_name(self) -> str:
        return self._model_name

    def generate_patch(
        self,
        context: MigrationTaskContext,
        prompt: str,
    ) -> PatchProposal:
        del context
        try:
            response = self._client.chat.completions.create(
                model=self._model_name,
                messages=[{"role": "user", "content": prompt}],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "patch_proposal",
                        "strict": True,
                        "schema": PatchProposal.model_json_schema(),
                    },
                },
                temperature=0,
            )
            message = response.choices[0].message
            refusal = getattr(message, "refusal", None)
            if refusal:
                raise ModelInvocationError(
                    "generate_patch",
                    f"AiHubMix model refused the request: {refusal}",
                    diagnostics={"model": self._model_name},
                )
            content = message.content
            if not isinstance(content, str) or not content.strip():
                raise ModelInvocationError(
                    "generate_patch",
                    "AiHubMix returned an empty structured response",
                    diagnostics={"model": self._model_name},
                )
            return PatchProposal.model_validate_json(content)
        except ModelInvocationError:
            raise
        except (ValidationError, ValueError, TypeError, AttributeError) as error:
            raise ModelInvocationError(
                "generate_patch",
                f"AiHubMix returned an invalid structured PatchProposal: {error}",
                diagnostics={"model": self._model_name},
            ) from error
        except Exception as error:
            raise ModelInvocationError(
                "generate_patch",
                f"AiHubMix request failed: {error}",
                diagnostics={"model": self._model_name},
            ) from error

    def generate_bundle_patch(
        self,
        context: MigrationBundleContext,
        prompt: str,
    ) -> BundlePatchProposal:
        del context
        try:
            response = self._client.chat.completions.create(
                model=self._model_name,
                messages=[{"role": "user", "content": prompt}],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "bundle_patch_proposal",
                        "strict": True,
                        "schema": BundlePatchProposal.model_json_schema(),
                    },
                },
                temperature=0,
            )
            message = response.choices[0].message
            refusal = getattr(message, "refusal", None)
            if refusal:
                raise ModelInvocationError(
                    "generate_bundle_patch",
                    f"model refused the request: {refusal}",
                    diagnostics={"model": self._model_name},
                )
            content = message.content
            if not isinstance(content, str) or not content.strip():
                raise ModelInvocationError(
                    "generate_bundle_patch",
                    "model returned an empty structured response",
                    diagnostics={"model": self._model_name},
                )
            return BundlePatchProposal.model_validate_json(content)
        except ModelInvocationError:
            raise
        except (ValidationError, ValueError, TypeError, AttributeError) as error:
            raise ModelInvocationError(
                "generate_bundle_patch",
                f"model returned an invalid BundlePatchProposal: {error}",
                diagnostics={"model": self._model_name},
            ) from error
        except Exception as error:
            raise ModelInvocationError(
                "generate_bundle_patch",
                f"bundle patch request failed: {error}",
                diagnostics={"model": self._model_name},
            ) from error

    def generate_file_bundle_patch(
        self,
        context: FileBundleExecutionContext,
        prompt: str,
    ) -> FilePatchProposal:
        del context
        try:
            response = self._client.chat.completions.create(
                model=self._model_name,
                messages=[{"role": "user", "content": prompt}],
                response_format={
                    "type": "json_schema",
                    "json_schema": {
                        "name": "file_patch_proposal",
                        "strict": True,
                        "schema": FilePatchProposal.model_json_schema(),
                    },
                },
                temperature=0,
            )
            message = response.choices[0].message
            refusal = getattr(message, "refusal", None)
            if refusal:
                raise ModelInvocationError(
                    "generate_file_bundle_patch",
                    f"model refused the request: {refusal}",
                    diagnostics={"model": self._model_name},
                )
            content = message.content
            if not isinstance(content, str) or not content.strip():
                raise ModelInvocationError(
                    "generate_file_bundle_patch",
                    "model returned an empty structured response",
                    diagnostics={"model": self._model_name},
                )
            return FilePatchProposal.model_validate_json(content)
        except ModelInvocationError:
            raise
        except (ValidationError, ValueError, TypeError, AttributeError) as error:
            raise ModelInvocationError(
                "generate_file_bundle_patch",
                f"model returned an invalid FilePatchProposal: {error}",
                diagnostics={"model": self._model_name},
            ) from error
        except Exception as error:
            raise ModelInvocationError(
                "generate_file_bundle_patch",
                f"file bundle patch request failed: {error}",
                diagnostics={"model": self._model_name},
            ) from error
