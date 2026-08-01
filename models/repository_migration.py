"""Typed repository-level migration orchestration results."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from models.go_validation import GoCommandResult, GoDiagnostic, ValidationBaseline


class RepositoryMigrationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RepositoryMigrationStatus(StrEnum):
    PLANNED = "planned"
    COMPLETED = "completed"
    COMPLETED_WITH_PREEXISTING_FAILURES = "completed_with_preexisting_failures"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class BundleMigrationRecord(RepositoryMigrationModel):
    bundle_id: str
    status: str
    proposal_path: str | None = None
    apply_result_path: str | None = None
    error: str | None = None


class RepositoryMigrationRun(RepositoryMigrationModel):
    repository_path: str
    status: RepositoryMigrationStatus
    baseline: ValidationBaseline | None
    bundle_order: list[str]
    bundle_records: list[BundleMigrationRecord]
    changed_files: list[str]
    dependency_files_changed: list[str]
    command_results: list[GoCommandResult]
    diagnostics: list[GoDiagnostic]
    introduced_diagnostics: list[GoDiagnostic]
    persistent_diagnostics: list[GoDiagnostic]
    resolved_diagnostics: list[GoDiagnostic]
    rollback_performed: bool
    rollback_succeeded: bool | None
    repair_required: bool
    errors: list[str]
