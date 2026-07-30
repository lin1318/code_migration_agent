"""Typed shared state and reports for deterministic LangGraph orchestration."""

from __future__ import annotations

from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict

from models.analyzer import AnalyzerResult
from models.inspection import RepositoryInspection
from models.migration_plan import MigrationInventory, MigrationTask, RepositorySummary
from models.migration_rules import MigrationRule

FinalStatus = Literal["success", "failed"]


class WorkflowModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkflowError(WorkflowModel):
    stage: str
    error_type: str
    message: str
    diagnostics: dict[str, str | int | float | None]


class RuleRegistryMetadata(WorkflowModel):
    path: str
    rule_count: int
    rule_ids: list[str]


class WorkflowReport(RepositoryInspection):
    completed_stages: list[str]
    warnings: list[str]
    errors: list[WorkflowError]
    final_status: FinalStatus


class WorkflowFailureReport(WorkflowModel):
    repository_path: str
    completed_stages: list[str]
    warnings: list[str]
    errors: list[WorkflowError]
    final_status: Literal["failed"]


class WorkflowState(TypedDict, total=False):
    repository_path: str
    rule_registry_path: str
    analyzer_timeout: float
    current_stage: str
    completed_stages: list[str]
    analyzer_result: AnalyzerResult | None
    migration_rules: list[MigrationRule]
    rule_registry_metadata: RuleRegistryMetadata | None
    migration_inventory: MigrationInventory | None
    migration_plan: list[MigrationTask] | None
    repository_summary: RepositorySummary | None
    warnings: list[str]
    errors: list[WorkflowError]
    final_report: WorkflowReport | WorkflowFailureReport | None
    final_status: FinalStatus | None
