"""Deterministic model client for tests and local demonstrations."""

from __future__ import annotations

from agent.execution.models import MigrationTaskContext
from models.bundle_execution import BundlePatchProposal, MigrationBundleContext
from models.file_bundle_execution import (
    FileBundleExecutionContext,
    FilePatchProposal,
)
from models.patch_proposal import PatchProposal


class FakeModelClient:
    def __init__(
        self,
        proposal: PatchProposal,
        *,
        model_name: str = "fake-deterministic",
    ) -> None:
        self._proposal = proposal
        self._model_name = model_name
        self.calls: list[tuple[MigrationTaskContext, str]] = []

    @property
    def model_name(self) -> str:
        return self._model_name

    def generate_patch(
        self,
        context: MigrationTaskContext,
        prompt: str,
    ) -> PatchProposal:
        self.calls.append((context, prompt))
        return self._proposal.model_copy(deep=True)


class FakeBundleModelClient:
    def __init__(
        self,
        proposal: BundlePatchProposal,
        *,
        model_name: str = "fake-bundle-deterministic",
    ) -> None:
        self._proposal = proposal
        self._model_name = model_name
        self.calls: list[tuple[MigrationBundleContext, str]] = []

    @property
    def model_name(self) -> str:
        return self._model_name

    def generate_bundle_patch(
        self,
        context: MigrationBundleContext,
        prompt: str,
    ) -> BundlePatchProposal:
        self.calls.append((context, prompt))
        return self._proposal.model_copy(deep=True)


class FakeFileBundleModelClient:
    def __init__(
        self,
        proposal: FilePatchProposal,
        *,
        model_name: str = "fake-file-bundle-deterministic",
    ) -> None:
        self._proposal = proposal
        self._model_name = model_name
        self.calls: list[tuple[FileBundleExecutionContext, str]] = []

    @property
    def model_name(self) -> str:
        return self._model_name

    def generate_file_bundle_patch(
        self,
        context: FileBundleExecutionContext,
        prompt: str,
    ) -> FilePatchProposal:
        self.calls.append((context, prompt))
        return self._proposal.model_copy(deep=True)
