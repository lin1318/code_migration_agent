"""Typed context and proposal models for one-file bundle generation."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field, model_validator

from agent.execution.models import RelevantSourceFile
from models.file_bundles import FileMigrationBundle, ReadOnlySymbolContext
from models.migration_plan import InventoryUsage, PlanModel, RepositorySummary
from models.migration_rules import MigrationRule
from models.patch_proposal import PatchConfidence


class FilePatchStatus(StrEnum):
    PATCH_GENERATED = "patch_generated"
    BLOCKED = "blocked"
    NO_CHANGE_NEEDED = "no_change_needed"
    FAILED = "failed"


class FilePatchProposal(PlanModel):
    bundle_id: str
    file: str
    status: FilePatchStatus
    summary: str = Field(min_length=1)
    patch: str | None
    confidence: PatchConfidence
    assumptions: list[str]
    manual_review_notes: list[str]
    unresolved_dependencies: list[str]

    @model_validator(mode="after")
    def validate_status_payload(self) -> "FilePatchProposal":
        if self.status is FilePatchStatus.PATCH_GENERATED:
            if self.patch is None or not self.patch.strip():
                raise ValueError("patch_generated requires a non-empty patch")
        elif self.patch is not None:
            raise ValueError(
                f"{self.status.value} requires a null patch"
            )
        if self.status is FilePatchStatus.BLOCKED and not (
            self.unresolved_dependencies or self.manual_review_notes
        ):
            raise ValueError(
                "blocked requires unresolved dependencies or review notes"
            )
        return self


class FileBundleExecutionContext(PlanModel):
    repository_summary: RepositorySummary
    bundle: FileMigrationBundle
    editable_file: RelevantSourceFile
    usages: list[InventoryUsage]
    migration_rules: list[MigrationRule]
    read_only_context: list[ReadOnlySymbolContext]
    completed_files: list[str]


class FileBundleExecutionResult(PlanModel):
    bundle_id: str
    file: str
    usage_count: int = Field(ge=0)
    migration_rule_count: int = Field(ge=0)
    read_only_symbol_count: int = Field(ge=0)
    prompt_size: int = Field(ge=0)
    model_used: str
    proposal: FilePatchProposal

