"""Typed Stage 9 bundle execution context and proposal models."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator

from agent.execution.models import (
    RelatedSymbolDefinition,
    RelevantLineRange,
    RelevantSourceFile,
)
from models.bundles import MigrationBundle
from models.migration_plan import RepositorySummary
from models.migration_rules import MigrationRule
from models.patch_proposal import PatchConfidence


class BundleExecutionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BundlePatchStatus(StrEnum):
    PATCH_GENERATED = "patch_generated"
    BLOCKED = "blocked"
    NO_CHANGE_NEEDED = "no_change_needed"
    FAILED = "failed"


class BundleFilePatch(BundleExecutionModel):
    file: str
    patch: str = Field(min_length=1)


class BundlePatchProposal(BundleExecutionModel):
    bundle_id: str
    status: BundlePatchStatus
    summary: str = Field(min_length=1)
    patches: list[BundleFilePatch]
    modified_files: list[str]
    confidence: PatchConfidence
    assumptions: list[str]
    manual_review_notes: list[str]
    unresolved_dependencies: list[str]

    @model_validator(mode="after")
    def validate_status_payload(self) -> "BundlePatchProposal":
        if self.status == BundlePatchStatus.PATCH_GENERATED and not self.patches:
            raise ValueError("patch_generated requires at least one patch")
        if self.status == BundlePatchStatus.NO_CHANGE_NEEDED and (
            self.patches or self.modified_files
        ):
            raise ValueError(
                "no_change_needed requires empty patches and modified_files"
            )
        return self


class MigrationBundleContext(BundleExecutionModel):
    repository_summary: RepositorySummary
    bundle: MigrationBundle
    allowed_files: list[str]
    framework_symbols: list[str]
    migration_concerns: list[str]
    relevant_source_files: list[RelevantSourceFile]
    relevant_source_ranges: list[RelevantLineRange]
    related_type_definitions: list[RelatedSymbolDefinition]
    related_function_signatures: list[RelatedSymbolDefinition]
    matching_migration_rules: list[MigrationRule]
    known_replacement_rules: list[MigrationRule]
    completed_bundles: list[str]
    pending_bundles: list[str]


class BundleExecutionResult(BundleExecutionModel):
    bundle_id: str
    allowed_file_count: int = Field(ge=0)
    source_range_count: int = Field(ge=0)
    related_symbol_count: int = Field(ge=0)
    migration_rule_count: int = Field(ge=0)
    prompt_size: int = Field(ge=0)
    model_used: str
    proposal: BundlePatchProposal
