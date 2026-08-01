"""Typed Stage 11 Go build and test validation models."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ValidationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ValidationLevel(StrEnum):
    FORMAT = "format"
    AFFECTED = "affected"
    REPOSITORY = "repository"
    FULL = "full"


class DiagnosticCategory(StrEnum):
    COMPILER_ERROR = "compiler_error"
    TEST_FAILURE = "test_failure"
    VET_ERROR = "vet_error"
    FORMATTING_ERROR = "formatting_error"
    DEPENDENCY_ERROR = "dependency_error"
    PACKAGE_LOADING_ERROR = "package_loading_error"
    COMMAND_TIMEOUT = "command_timeout"
    COMMAND_EXECUTION_ERROR = "command_execution_error"
    UNKNOWN_ERROR = "unknown_error"


class DiagnosticClassification(StrEnum):
    PRE_EXISTING = "pre_existing"
    INTRODUCED = "introduced"
    PERSISTENT = "persistent"
    RESOLVED = "resolved"
    UNCLASSIFIED = "unclassified"


class CommandStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    SKIPPED = "skipped"


class BundleValidationStatus(StrEnum):
    PASSED = "passed"
    PASSED_WITH_PREEXISTING_FAILURES = "passed_with_preexisting_failures"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    INVALID_STATE = "invalid_state"


class ValidationCommandConfig(ValidationModel):
    operation: str
    enabled: bool = True
    timeout_seconds: float = Field(gt=0)
    required: bool = True


class GoCommandResult(ValidationModel):
    operation: str
    command: list[str]
    working_directory: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float = Field(ge=0)
    timed_out: bool
    started: str
    completed: str
    skipped: bool = False
    skip_reason: str | None = None

    @property
    def status(self) -> CommandStatus:
        if self.skipped:
            return CommandStatus.SKIPPED
        if self.timed_out:
            return CommandStatus.TIMED_OUT
        return CommandStatus.PASSED if self.exit_code == 0 else CommandStatus.FAILED


class GoDiagnostic(ValidationModel):
    diagnostic_id: str
    category: DiagnosticCategory
    operation: str
    package: str | None = None
    file: str | None = None
    line: int | None = None
    column: int | None = None
    test_name: str | None = None
    message: str
    raw_line: str
    command_index: int
    normalized_key: str
    classification: DiagnosticClassification = (
        DiagnosticClassification.UNCLASSIFIED
    )


class PackageValidationResult(ValidationModel):
    package: str
    build_status: CommandStatus
    test_status: CommandStatus
    diagnostics: list[GoDiagnostic]


class ValidationSummary(ValidationModel):
    command_count: int = Field(ge=0)
    passed_command_count: int = Field(ge=0)
    failed_command_count: int = Field(ge=0)
    timed_out_command_count: int = Field(ge=0)
    skipped_command_count: int = Field(ge=0)
    diagnostic_count: int = Field(ge=0)


class ValidationBaseline(ValidationModel):
    repository_revision: str
    repository_status: list[str]
    command_results: list[GoCommandResult]
    diagnostics: list[GoDiagnostic]
    package_results: list[PackageValidationResult]
    summary: ValidationSummary


class BundleValidationResult(ValidationModel):
    bundle_id: str
    status: BundleValidationStatus
    repository_path: str
    validation_level: ValidationLevel
    affected_packages: list[str]
    formatted_files: list[str]
    dependency_files_changed: list[str]
    command_results: list[GoCommandResult]
    diagnostics: list[GoDiagnostic]
    introduced_diagnostics: list[GoDiagnostic]
    persistent_diagnostics: list[GoDiagnostic]
    resolved_diagnostics: list[GoDiagnostic]
    unclassified_diagnostics: list[GoDiagnostic]
    package_results: list[PackageValidationResult]
    repository_diff: str
    summary: ValidationSummary
    repair_required: bool
