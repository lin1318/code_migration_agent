"""Typed results for validation after ordered file migration."""

from __future__ import annotations

from enum import StrEnum

from models.go_validation import GoCommandResult
from models.file_migration_execution import FileMigrationRunResult
from models.migration_plan import PlanModel


class FileMigrationWorkflowStatus(StrEnum):
    COMPLETED = "completed"
    MIGRATION_STOPPED = "migration_stopped"
    VALIDATION_FAILED = "validation_failed"


class FileMigrationValidationResult(PlanModel):
    status: FileMigrationWorkflowStatus
    formatted_files: list[str]
    dependency_files_changed: list[str]
    command_results: list[GoCommandResult]


class FileMigrationWorkflowReport(PlanModel):
    repository_path: str
    status: FileMigrationWorkflowStatus
    completed_stages: list[str]
    migration: FileMigrationRunResult
    validation: FileMigrationValidationResult | None
