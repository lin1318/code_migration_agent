"""Structured model output for one migration task."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PatchProposalStatus(StrEnum):
    PATCH_GENERATED = "patch_generated"
    BLOCKED = "blocked"
    NO_CHANGE_NEEDED = "no_change_needed"
    FAILED = "failed"


class PatchConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class PatchProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str = Field(min_length=1)
    status: PatchProposalStatus
    summary: str = Field(min_length=1)
    files_to_modify: list[str]
    patch: str | None
    confidence: PatchConfidence
    assumptions: list[str]
    manual_review_notes: list[str]
    unresolved_dependencies: list[str]

    @model_validator(mode="after")
    def validate_status_payload(self) -> PatchProposal:
        if self.status == PatchProposalStatus.PATCH_GENERATED:
            if self.patch is None or not self.patch.strip():
                raise ValueError("patch_generated requires a non-empty patch")
            if not self.files_to_modify:
                raise ValueError(
                    "patch_generated requires at least one file_to_modify"
                )
        elif (
            self.status == PatchProposalStatus.NO_CHANGE_NEEDED
            and self.patch is not None
        ):
            raise ValueError("no_change_needed requires a null patch")
        if (
            self.status == PatchProposalStatus.BLOCKED
            and not self.summary.strip()
            and not self.manual_review_notes
        ):
            raise ValueError("blocked requires an explanation")
        return self
