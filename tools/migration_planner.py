"""Deterministic inventory and planning from analyzer output and rule knowledge."""

from __future__ import annotations

import hashlib
from collections import Counter

from knowledge.registry import RuleRegistry
from models.analyzer import AnalyzerResult, GinUsage
from models.migration_plan import (
    DerivedTaskInformation,
    InventoryUsage,
    MigrationInventory,
    MigrationPhase,
    MigrationTask,
    MigrationTaskKind,
    PlannedMigration,
    RepositorySummary,
    UnsupportedUsage,
)
from models.migration_rules import MigrationCategory, MigrationConfidence, MigrationRule

_GIN_PACKAGE = "github.com/gin-gonic/gin"
_PHASES = tuple(MigrationPhase)
_PRIMARY_PHASES = _PHASES[1:8]

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
    MigrationCategory.ROUTING: MigrationPhase.ROUTING,
    MigrationCategory.CONTEXT_TYPE: MigrationPhase.HANDLER_SIGNATURES,
    MigrationCategory.HANDLER_TYPE: MigrationPhase.HANDLER_SIGNATURES,
    MigrationCategory.REQUEST_ACCESS: MigrationPhase.CONTEXT_OPERATIONS,
    MigrationCategory.DATA_BINDING: MigrationPhase.REQUEST_BINDING,
    MigrationCategory.RESPONSE_RENDERING: MigrationPhase.RESPONSES,
    MigrationCategory.DATA_STRUCTURE: MigrationPhase.RESPONSES,
    MigrationCategory.MIDDLEWARE_CONTROL: MigrationPhase.MIDDLEWARE,
}

_USAGE_KIND_PHASE = {
    "constructor": MigrationPhase.SERVER_BOOTSTRAP,
    "engine_method": MigrationPhase.ROUTING,
    "router_group_method": MigrationPhase.ROUTING,
    "group": MigrationPhase.ROUTING,
    "gin_type": MigrationPhase.HANDLER_SIGNATURES,
    "context_method": MigrationPhase.CONTEXT_OPERATIONS,
    "gin_h": MigrationPhase.RESPONSES,
    "middleware": MigrationPhase.MIDDLEWARE,
}


def build_migration_inventory(
    analysis: AnalyzerResult,
    registry: RuleRegistry,
) -> MigrationInventory:
    """Build a stable inventory without executing tools or changing repositories."""

    usages = _inventory_usages(analysis.usages)
    exact: list[PlannedMigration] = []
    review: list[PlannedMigration] = []
    unsupported: list[UnsupportedUsage] = []
    unmatched: list[UnsupportedUsage] = []
    rules_by_usage_id: dict[str, MigrationRule] = {}

    for usage in usages:
        if usage.kind == "gin_import":
            continue
        rule = registry.lookup_resolved_symbol(usage.symbol)
        if rule is None:
            unmatched.append(
                UnsupportedUsage(
                    usage=usage,
                    reason="no exact migration rule for resolved symbol",
                )
            )
            continue

        rules_by_usage_id[usage.usage_id] = rule
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
        if rule.confidence is MigrationConfidence.EXACT:
            exact.append(migration)
        else:
            review.append(migration)

    inventory = MigrationInventory(
        repository_summary=_repository_summary(analysis),
        discovered_gin_usages=usages,
        exact_migrations=exact,
        migrations_requiring_review=review,
        unsupported_usages=unsupported,
        unmatched_usages=unmatched,
        tasks=_build_tasks(usages, rules_by_usage_id),
    )
    validate_migration_inventory(inventory)
    return inventory


def validate_migration_inventory(inventory: MigrationInventory) -> None:
    """Validate classification and primary task ownership invariants."""

    validate_inventory_classification(inventory)
    validate_migration_plan(
        inventory.tasks,
        inventory.discovered_gin_usages,
    )


def validate_inventory_classification(inventory: MigrationInventory) -> None:
    """Validate semantic usage identity and classification coverage."""

    usage_ids = [usage.usage_id for usage in inventory.discovered_gin_usages]
    if len(usage_ids) != len(set(usage_ids)):
        raise ValueError("discovered Gin usage IDs are not unique")
    summary = inventory.repository_summary
    if summary.gin_import_count + summary.gin_api_usage_count != summary.gin_usage_count:
        raise ValueError("Gin import and semantic API counts do not equal total usages")

    api_ids = {
        usage.usage_id
        for usage in inventory.discovered_gin_usages
        if usage.kind != "gin_import"
    }
    classified_id_list = [
        item.usage.usage_id
        for group in (
            inventory.exact_migrations,
            inventory.migrations_requiring_review,
            inventory.unsupported_usages,
            inventory.unmatched_usages,
        )
        for item in group
    ]
    if len(classified_id_list) != len(set(classified_id_list)):
        raise ValueError("an API usage appears in multiple classification groups")
    if set(classified_id_list) != api_ids:
        raise ValueError("API classification does not exactly cover non-import Gin usages")


def get_ordered_migration_plan(
    inventory: MigrationInventory,
) -> list[MigrationTask]:
    """Return the deterministic ordered plan already built for an inventory."""

    return list(inventory.tasks)


def validate_migration_plan(
    tasks: list[MigrationTask],
    usages: list[InventoryUsage],
) -> None:
    """Validate ordering and exclusive primary usage ownership."""

    expected_phases = list(_PHASES)
    if [task.phase for task in tasks] != expected_phases:
        raise ValueError("migration tasks do not follow the required phase order")
    if [task.order for task in tasks] != list(range(1, 11)):
        raise ValueError("migration task order values must be 1 through 10")

    api_ids = {
        usage.usage_id
        for usage in usages
        if usage.kind != "gin_import"
    }
    direct_ids = [
        usage_id
        for task in tasks
        if task.task_kind is MigrationTaskKind.PRIMARY
        for usage_id in task.direct_usage_ids
    ]
    duplicates = sorted(
        usage_id for usage_id, count in Counter(direct_ids).items() if count > 1
    )
    if duplicates:
        raise ValueError(f"direct usages have multiple primary owners: {duplicates}")
    if set(direct_ids) != api_ids:
        raise ValueError("primary task usages do not exactly cover non-import Gin usages")

    for task in tasks:
        if task.task_kind is MigrationTaskKind.PRIMARY:
            if task.derived_from_usage_ids or task.derived_information:
                raise ValueError("primary tasks cannot claim derived usage information")
        elif task.task_kind is MigrationTaskKind.DERIVED:
            if task.direct_usage_ids:
                raise ValueError("derived tasks cannot own direct usages")
        elif (
            task.direct_usage_ids
            or task.derived_from_usage_ids
            or task.derived_information
        ):
            raise ValueError("workflow-only tasks cannot reference usages")

def _repository_summary(analysis: AnalyzerResult) -> RepositorySummary:
    source_files = sorted(
        {file for package in analysis.packages for file in package.source_files}
    )
    imports = sorted(
        {path for package in analysis.packages for path in package.imports}
    )
    package_paths = sorted(package.import_path for package in analysis.packages)
    import_count = sum(usage.kind == "gin_import" for usage in analysis.usages)
    return RepositorySummary(
        module_path=analysis.module_path,
        package_count=len(analysis.packages),
        package_import_paths=package_paths,
        source_file_count=len(source_files),
        source_files=source_files,
        import_count=len(imports),
        imports=imports,
        gin_usage_count=len(analysis.usages),
        gin_import_count=import_count,
        gin_api_usage_count=len(analysis.usages) - import_count,
        package_loading_error_count=len(analysis.loading_errors),
    )


def _planned_migration(
    usage: InventoryUsage, rule: MigrationRule
) -> PlannedMigration:
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
    usages: list[InventoryUsage],
    rules_by_usage_id: dict[str, MigrationRule],
) -> list[MigrationTask]:
    api_usages = [usage for usage in usages if usage.kind != "gin_import"]
    phase_usages: dict[MigrationPhase, list[InventoryUsage]] = {
        phase: [] for phase in _PRIMARY_PHASES
    }
    phase_rules: dict[MigrationPhase, list[MigrationRule]] = {
        phase: [] for phase in _PRIMARY_PHASES
    }

    for usage in api_usages:
        rule = rules_by_usage_id.get(usage.usage_id)
        phase = (
            _CATEGORY_PHASE[rule.migration_category]
            if rule is not None
            else _USAGE_KIND_PHASE.get(usage.kind)
        )
        if phase is None:
            raise ValueError(
                f"no primary migration phase for usage kind {usage.kind!r}"
            )
        phase_usages[phase].append(usage)
        if rule is not None:
            phase_rules[phase].append(rule)

    derived_information = _import_cleanup_information(
        api_usages, rules_by_usage_id
    )
    migratable_ids = {
        usage.usage_id
        for usage in api_usages
        if (
            (rule := rules_by_usage_id.get(usage.usage_id)) is not None
            and rule.confidence is not MigrationConfidence.UNSUPPORTED
        )
    }
    dependency_rules = [
        rule
        for usage_id, rule in rules_by_usage_id.items()
        if usage_id in migratable_ids
    ]

    tasks: list[MigrationTask] = []
    for order, phase in enumerate(_PHASES, start=1):
        if phase is MigrationPhase.DEPENDENCIES:
            tasks.append(
                MigrationTask(
                    order=order,
                    phase=phase,
                    task_kind=MigrationTaskKind.DERIVED,
                    description=_DESCRIPTIONS[phase],
                    usage_count=0,
                    direct_usage_ids=[],
                    derived_from_usage_ids=sorted(migratable_ids),
                    derived_information=derived_information,
                    source_symbols=[],
                    rule_ids=sorted({rule.rule_id for rule in dependency_rules}),
                    required_imports=sorted(
                        {
                            item
                            for rule in dependency_rules
                            for item in rule.required_imports
                        }
                    ),
                )
            )
            continue
        if phase in (MigrationPhase.TESTS, MigrationPhase.FINAL_VERIFICATION):
            tasks.append(
                MigrationTask(
                    order=order,
                    phase=phase,
                    task_kind=MigrationTaskKind.WORKFLOW,
                    description=_DESCRIPTIONS[phase],
                    usage_count=0,
                    direct_usage_ids=[],
                    derived_from_usage_ids=[],
                    derived_information=[],
                    source_symbols=[],
                    rule_ids=[],
                    required_imports=[],
                )
            )
            continue

        selected_usages = phase_usages[phase]
        selected_rules = phase_rules[phase]
        tasks.append(
            MigrationTask(
                order=order,
                phase=phase,
                task_kind=MigrationTaskKind.PRIMARY,
                description=_DESCRIPTIONS[phase],
                usage_count=len(selected_usages),
                direct_usage_ids=sorted(
                    usage.usage_id for usage in selected_usages
                ),
                derived_from_usage_ids=[],
                derived_information=[],
                source_symbols=sorted(
                    {usage.symbol for usage in selected_usages}
                ),
                rule_ids=sorted({rule.rule_id for rule in selected_rules}),
                required_imports=[],
            )
        )
    return tasks


def _import_cleanup_information(
    api_usages: list[InventoryUsage],
    rules_by_usage_id: dict[str, MigrationRule],
) -> list[DerivedTaskInformation]:
    by_file: dict[str, list[InventoryUsage]] = {}
    for usage in api_usages:
        by_file.setdefault(usage.file, []).append(usage)

    information: list[DerivedTaskInformation] = []
    for file, file_usages in sorted(by_file.items()):
        migratable = [
            usage
            for usage in file_usages
            if (
                (rule := rules_by_usage_id.get(usage.usage_id)) is not None
                and rule.confidence is not MigrationConfidence.UNSUPPORTED
            )
        ]
        if not migratable:
            continue
        unresolved = len(migratable) != len(file_usages)
        imports = sorted(
            {
                required_import
                for usage in migratable
                for required_import in rules_by_usage_id[
                    usage.usage_id
                ].required_imports
            }
        )
        information.append(
            DerivedTaskInformation(
                kind="import_cleanup",
                file=file,
                derived_from_usage_ids=sorted(
                    usage.usage_id for usage in migratable
                ),
                remove_imports=[] if unresolved else [_GIN_PACKAGE],
                add_imports=imports,
                notes=(
                    "Retain the Gin import until unmatched or unsupported usages "
                    "in this file are resolved."
                    if unresolved
                    else "Remove the Gin import after all direct usages are migrated."
                ),
            )
        )
    return information


def _inventory_usages(usages: list[GinUsage]) -> list[InventoryUsage]:
    inventory = [
        InventoryUsage(
            **usage.model_dump(),
            usage_id=_usage_id(usage),
        )
        for usage in sorted(usages, key=_usage_key)
    ]
    ids = [usage.usage_id for usage in inventory]
    if len(ids) != len(set(ids)):
        raise ValueError("discovered Gin usages do not have unique stable IDs")
    return inventory


def _usage_id(usage: GinUsage) -> str:
    identity = "\0".join(map(str, _usage_key(usage))).encode()
    return "gin-" + hashlib.sha256(identity).hexdigest()[:16]


def _usage_key(usage: GinUsage) -> tuple[str, int, int, str, str]:
    return usage.file, usage.line, usage.column, usage.kind, usage.symbol
