"""Deterministic inventory and planning from analyzer output and rule knowledge."""

from __future__ import annotations

from collections.abc import Iterable

from knowledge.registry import RuleRegistry
from models.analyzer import AnalyzerResult, GinUsage
from models.migration_plan import (
    MigrationInventory,
    MigrationPhase,
    MigrationTask,
    PlannedMigration,
    RepositorySummary,
    UnsupportedUsage,
)
from models.migration_rules import MigrationCategory, MigrationConfidence, MigrationRule


_PHASES = (
    MigrationPhase.DEPENDENCIES,
    MigrationPhase.SERVER_BOOTSTRAP,
    MigrationPhase.ROUTING,
    MigrationPhase.HANDLER_SIGNATURES,
    MigrationPhase.CONTEXT_OPERATIONS,
    MigrationPhase.REQUEST_BINDING,
    MigrationPhase.RESPONSES,
    MigrationPhase.MIDDLEWARE,
    MigrationPhase.TESTS,
    MigrationPhase.FINAL_VERIFICATION,
)

_DESCRIPTIONS = {
    MigrationPhase.DEPENDENCIES: "Update Go module dependencies and required imports.",
    MigrationPhase.SERVER_BOOTSTRAP: "Migrate server construction and bootstrap.",
    MigrationPhase.ROUTING: "Migrate route and route-group registration.",
    MigrationPhase.HANDLER_SIGNATURES: "Adapt handler and context type signatures.",
    MigrationPhase.CONTEXT_OPERATIONS: "Migrate request-context access operations.",
    MigrationPhase.REQUEST_BINDING: "Migrate request binding and validation.",
    MigrationPhase.RESPONSES: "Migrate response rendering and response data structures.",
    MigrationPhase.MIDDLEWARE: "Migrate middleware flow and chain control.",
    MigrationPhase.TESTS: "Add or update tests for migrated behavior.",
    MigrationPhase.FINAL_VERIFICATION: "Run gofmt, go test ./..., and go vet ./....",
}

_CATEGORY_PHASE = {
    MigrationCategory.SERVER_INITIALIZATION: MigrationPhase.SERVER_BOOTSTRAP,
    MigrationCategory.CONTEXT_TYPE: MigrationPhase.HANDLER_SIGNATURES,
    MigrationCategory.HANDLER_TYPE: MigrationPhase.HANDLER_SIGNATURES,
    MigrationCategory.REQUEST_ACCESS: MigrationPhase.CONTEXT_OPERATIONS,
    MigrationCategory.DATA_BINDING: MigrationPhase.REQUEST_BINDING,
    MigrationCategory.RESPONSE_RENDERING: MigrationPhase.RESPONSES,
    MigrationCategory.DATA_STRUCTURE: MigrationPhase.RESPONSES,
    MigrationCategory.MIDDLEWARE_CONTROL: MigrationPhase.MIDDLEWARE,
}

_USAGE_KIND_PHASE = {
    "engine_method": MigrationPhase.ROUTING,
    "router_group_method": MigrationPhase.ROUTING,
    "group": MigrationPhase.ROUTING,
    "middleware": MigrationPhase.MIDDLEWARE,
}


def build_migration_inventory(
    analysis: AnalyzerResult,
    registry: RuleRegistry,
) -> MigrationInventory:
    """Build a stable inventory without executing tools or changing repositories."""

    usages = sorted(analysis.usages, key=_usage_key)
    exact: list[PlannedMigration] = []
    review: list[PlannedMigration] = []
    unsupported: list[UnsupportedUsage] = []
    matched: list[tuple[GinUsage, MigrationRule]] = []

    for usage in usages:
        rule = registry.lookup_resolved_symbol(usage.symbol)
        if rule is None:
            unsupported.append(
                UnsupportedUsage(
                    usage=usage,
                    reason="no exact migration rule for resolved symbol",
                )
            )
            continue
        if rule.confidence is MigrationConfidence.UNSUPPORTED:
            unsupported.append(
                UnsupportedUsage(
                    usage=usage,
                    rule_id=rule.rule_id,
                    reason=rule.notes,
                )
            )
            continue

        migration = _planned_migration(usage, rule)
        matched.append((usage, rule))
        if rule.confidence is MigrationConfidence.EXACT:
            exact.append(migration)
        else:
            review.append(migration)

    return MigrationInventory(
        repository_summary=_repository_summary(analysis),
        discovered_gin_usages=usages,
        exact_migrations=exact,
        migrations_requiring_review=review,
        unsupported_usages=unsupported,
        tasks=_build_tasks(usages, matched),
    )


def _repository_summary(analysis: AnalyzerResult) -> RepositorySummary:
    source_files = sorted(
        {file for package in analysis.packages for file in package.source_files}
    )
    imports = sorted(
        {path for package in analysis.packages for path in package.imports}
    )
    package_paths = sorted(package.import_path for package in analysis.packages)
    return RepositorySummary(
        module_path=analysis.module_path,
        package_count=len(analysis.packages),
        package_import_paths=package_paths,
        source_file_count=len(source_files),
        source_files=source_files,
        import_count=len(imports),
        imports=imports,
        gin_usage_count=len(analysis.usages),
        package_loading_error_count=len(analysis.loading_errors),
    )


def _planned_migration(usage: GinUsage, rule: MigrationRule) -> PlannedMigration:
    return PlannedMigration(
        usage=usage,
        rule_id=rule.rule_id,
        target_package=rule.target_package,
        target_symbol=rule.target_symbol,
        target_operations=rule.target_operations,
        migration_category=rule.migration_category,
        confidence=rule.confidence,
        required_imports=sorted(set(rule.required_imports)),
        notes=rule.notes,
    )


def _build_tasks(
    usages: list[GinUsage],
    matched: list[tuple[GinUsage, MigrationRule]],
) -> list[MigrationTask]:
    phase_usages: dict[MigrationPhase, list[GinUsage]] = {
        phase: [] for phase in _PHASES
    }
    phase_rules: dict[MigrationPhase, list[MigrationRule]] = {
        phase: [] for phase in _PHASES
    }

    for usage in usages:
        if phase := _USAGE_KIND_PHASE.get(usage.kind):
            phase_usages[phase].append(usage)
    for usage, rule in matched:
        phase = _CATEGORY_PHASE[rule.migration_category]
        phase_usages[phase].append(usage)
        phase_rules[phase].append(rule)
        phase_usages[MigrationPhase.DEPENDENCIES].append(usage)
        phase_rules[MigrationPhase.DEPENDENCIES].append(rule)

    tasks: list[MigrationTask] = []
    for order, phase in enumerate(_PHASES, start=1):
        selected_usages = _unique_usages(phase_usages[phase])
        selected_rules = phase_rules[phase]
        imports = (
            sorted({item for rule in selected_rules for item in rule.required_imports})
            if phase is MigrationPhase.DEPENDENCIES
            else []
        )
        tasks.append(
            MigrationTask(
                order=order,
                phase=phase,
                description=_DESCRIPTIONS[phase],
                usage_count=len(selected_usages),
                source_symbols=sorted({usage.symbol for usage in selected_usages}),
                rule_ids=sorted({rule.rule_id for rule in selected_rules}),
                required_imports=imports,
            )
        )
    return tasks


def _unique_usages(usages: Iterable[GinUsage]) -> list[GinUsage]:
    unique = {_usage_key(usage): usage for usage in usages}
    return [unique[key] for key in sorted(unique)]


def _usage_key(usage: GinUsage) -> tuple[str, int, int, str, str]:
    return usage.file, usage.line, usage.column, usage.kind, usage.symbol
