"""Typed results for ordered execution of one-file migration bundles."""

from __future__ import annotations

from enum import StrEnum

from pydantic import Field

from agent.execution.errors import ExecutionErrorDetail
from models.file_bundle_execution import FileBundleExecutionResult
from models.migration_plan import PlanModel


class FileBundleRunStatus(StrEnum):
    APPLIED = "applied"
    NO_CHANGE = "no_change"
    BLOCKED = "blocked"
    FAILED = "failed"
    ROLLED_BACK = "rolled_back"


class FileMigrationRunStatus(StrEnum):
    COMPLETED = "completed"
    STOPPED = "stopped"


class FileContentApplyStatus(StrEnum):
    APPLIED = "applied"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


class FileContentApplyResult(PlanModel):
    bundle_id: str
    file: str
    status: FileContentApplyStatus
    original_sha256: str
    updated_sha256: str | None = None
    audit_diff: str
    rollback_performed: bool = False
    rollback_succeeded: bool | None = None
    errors: list[str]


class FileBundleRunRecord(PlanModel):
    bundle_id: str
    file: str
    status: FileBundleRunStatus
    execution: FileBundleExecutionResult | None = None
    application: FileContentApplyResult | None = None
    error: ExecutionErrorDetail | None = None


class FileMigrationRunResult(PlanModel):
    repository_path: str
    status: FileMigrationRunStatus
    bundle_count: int = Field(ge=0)
    completed_files: list[str]
    records: list[FileBundleRunRecord]
    failed_bundle_id: str | None = None
