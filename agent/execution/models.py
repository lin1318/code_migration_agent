"""Typed execution context and results."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from models.migration_plan import (
    InventoryUsage,
    MigrationTask,
    RepositorySummary,
)
from models.migration_rules import MigrationRule
from models.patch_proposal import PatchProposal


class ExecutionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class NumberedSourceLine(ExecutionModel):
    number: int = Field(ge=1)
    text: str


class RelevantLineRange(ExecutionModel):
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)
    lines: list[NumberedSourceLine]


class RelevantSourceFile(ExecutionModel):
    path: str
    content: str


class RelatedSymbolDefinition(ExecutionModel):
    symbol: str
    path: str
    line: int = Field(ge=1)
    signature: str


class PendingDependency(ExecutionModel):
    phase: str
    task_order: int
    description: str


class MigrationTaskContext(ExecutionModel):
    repository_summary: RepositorySummary
    migration_task_id: str
    selected_migration_task: MigrationTask
    allowed_files: list[str]
    direct_usages: list[InventoryUsage]
    matching_migration_rules: list[MigrationRule]
    relevant_source_files: list[RelevantSourceFile]
    explicitly_updated_source_files: list[str]
    relevant_line_ranges: list[RelevantLineRange]
    related_symbol_definitions: list[RelatedSymbolDefinition]
    completed_migration_phases: list[str]
    pending_dependencies: list[PendingDependency]


class ContextSummary(ExecutionModel):
    allowed_file_count: int = Field(ge=0)
    direct_usage_count: int = Field(ge=0)
    migration_rule_count: int = Field(ge=0)
    source_file_count: int = Field(ge=0)
    line_range_count: int = Field(ge=0)
    related_symbol_count: int = Field(ge=0)


class TaskExecutionResult(ExecutionModel):
    selected_task: MigrationTask
    context_summary: ContextSummary
    prompt_size: int = Field(ge=0)
    model_used: str
    proposal: PatchProposal
