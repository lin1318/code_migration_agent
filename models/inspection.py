"""Structured repository inspection result."""

from pydantic import Field

from models.analyzer import SymbolReference
from models.migration_plan import (
    InventoryUsage,
    MigrationInventory,
    MigrationTask,
    PlanModel,
    PlannedMigration,
    RepositorySummary,
    UnsupportedUsage,
)


class RepositoryInspection(PlanModel):
    repository_path: str
    repository_summary: RepositorySummary
    packages_containing_gin_usages: list[str]
    discovered_gin_usages: list[InventoryUsage]
    exact_migrations: list[PlannedMigration]
    migrations_requiring_review: list[PlannedMigration]
    unsupported_usages: list[UnsupportedUsage]
    unmatched_usages: list[UnsupportedUsage]
    ordered_migration_tasks: list[MigrationTask] = Field(
        min_length=10, max_length=10
    )
    semantic_references: list[SymbolReference] = Field(default_factory=list)

    @classmethod
    def from_inventory(
        cls,
        repository_path: str,
        packages_containing_gin_usages: list[str],
        inventory: MigrationInventory,
        semantic_references: list[SymbolReference] | None = None,
    ) -> "RepositoryInspection":
        return cls(
            repository_path=repository_path,
            repository_summary=inventory.repository_summary,
            packages_containing_gin_usages=packages_containing_gin_usages,
            discovered_gin_usages=inventory.discovered_gin_usages,
            exact_migrations=inventory.exact_migrations,
            migrations_requiring_review=inventory.migrations_requiring_review,
            unsupported_usages=inventory.unsupported_usages,
            unmatched_usages=inventory.unmatched_usages,
            ordered_migration_tasks=inventory.tasks,
            semantic_references=semantic_references or [],
        )
