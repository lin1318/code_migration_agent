"""Provider-independent patch model contract."""

from __future__ import annotations

from typing import Protocol

from agent.execution.models import MigrationTaskContext
from models.patch_proposal import PatchProposal


class ModelClient(Protocol):
    @property
    def model_name(self) -> str: ...

    def generate_patch(
        self,
        context: MigrationTaskContext,
        prompt: str,
    ) -> PatchProposal: ...
