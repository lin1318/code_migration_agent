"""Validated output models for deterministic migration planning."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from models.analyzer import GinUsage
from models.migration_rules import (
    MigrationCategory,
    MigrationConfidence,
    TargetOperation,
)


class PlanModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RepositorySummary(PlanModel):
    module_path: str
    package_count: int = Field(ge=0)
    package_import_paths: list[str]
    source_file_count: int = Field(ge=0)
    source_files: list[str]
    import_count: int = Field(ge=0)
    imports: list[str]
    gin_usage_count: int = Field(ge=0)
    package_loading_error_count: int = Field(ge=0)


class PlannedMigration(PlanModel):
    usage: GinUsage
    rule_id: str
    target_package: str
    target_symbol: str | None
    target_operations: list[TargetOperation] | None
    migration_category: MigrationCategory
    confidence: MigrationConfidence
    required_imports: list[str]
    notes: str


class UnsupportedUsage(PlanModel):
    usage: GinUsage
    rule_id: str | None = None
    reason: str


class MigrationPhase(StrEnum):
    DEPENDENCIES = "dependencies"
    SERVER_BOOTSTRAP = "server_bootstrap"
    ROUTING = "routing"
    HANDLER_SIGNATURES = "handler_signatures"
    CONTEXT_OPERATIONS = "context_operations"
    REQUEST_BINDING = "request_binding"
    RESPONSES = "responses"
    MIDDLEWARE = "middleware"
    TESTS = "tests"
    FINAL_VERIFICATION = "final_verification"


class MigrationTask(PlanModel):
    order: int = Field(ge=1, le=10)
    phase: MigrationPhase
    description: str
    usage_count: int = Field(ge=0)
    source_symbols: list[str]
    rule_ids: list[str]
    required_imports: list[str]


class MigrationInventory(PlanModel):
    repository_summary: RepositorySummary
    discovered_gin_usages: list[GinUsage]
    exact_migrations: list[PlannedMigration]
    migrations_requiring_review: list[PlannedMigration]
    unsupported_usages: list[UnsupportedUsage]
    tasks: list[MigrationTask] = Field(min_length=10, max_length=10)
