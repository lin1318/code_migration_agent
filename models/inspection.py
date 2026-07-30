"""Structured repository inspection result."""

from pydantic import Field

from models.analyzer import GinUsage
from models.migration_plan import (
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
    discovered_gin_usages: list[GinUsage]
    exact_migrations: list[PlannedMigration]
    migrations_requiring_review: list[PlannedMigration]
    unsupported_usages: list[UnsupportedUsage]
    unmatched_usages: list[UnsupportedUsage]
    ordered_migration_tasks: list[MigrationTask] = Field(
        min_length=10, max_length=10
    )

    @classmethod
    def from_inventory(
        cls,
        repository_path: str,
        packages_containing_gin_usages: list[str],
        inventory: MigrationInventory,
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
        )
