"""Typed state for the LangGraph-managed file migration workflow."""

from __future__ import annotations

from typing import TypedDict

from models.file_bundles import FileBundlePlan
from models.file_classification import GinFileClassification
from models.file_migration_execution import FileMigrationRunResult
from models.file_migration_plan import FileMigrationPlan
from models.file_migration_validation import (
    FileMigrationValidationResult,
    FileMigrationWorkflowReport,
)
from models.inspection import RepositoryInspection
from models.migration_rules import MigrationRule
from models.workflow import WorkflowError


class FileMigrationGraphState(TypedDict, total=False):
    repository_path: str
    output_directory: str
    rule_registry_path: str
    analyzer_timeout: float
    apply_timeout: float
    validation_timeout: float
    allow_dirty: bool
    current_stage: str
    completed_stages: list[str]
    errors: list[WorkflowError]
    inspection: RepositoryInspection | None
    migration_rules: list[MigrationRule]
    classification: GinFileClassification | None
    file_plan: FileMigrationPlan | None
    bundle_plan: FileBundlePlan | None
    execution: FileMigrationRunResult | None
    validation: FileMigrationValidationResult | None
    final_report: FileMigrationWorkflowReport | None
    final_status: str | None
