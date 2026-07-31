"""Deterministic semantic dependency resolution for one migration task."""

from __future__ import annotations

import hashlib
from collections import defaultdict

from agent.execution.context_builder import stable_task_id
from agent.execution.errors import TaskSelectionError
from knowledge.registry import RuleRegistry
from models.analyzer import SymbolReference
from models.dependencies import (
    DependencyGraph,
    DependencyGraphEdge,
    DependencyGraphNode,
    DependencyKind,
    DependencyRef,
    DependencyResolution,
    GeneratedChildTask,
    TaskReadinessResult,
    TaskReadinessStatus,
)
from models.inspection import RepositoryInspection
from models.migration_plan import MigrationPhase, MigrationTask
from models.migration_rules import ResolutionStrategy

GIN_PACKAGE = "github.com/gin-gonic/gin"
MAX_DEPENDENCY_DEPTH = 8


def select_dependency_task(
    inspection: RepositoryInspection,
    *,
    phase: str,
    task_order: int | None = None,
) -> MigrationTask:
    try:
        canonical_phase = MigrationPhase(phase)
    except ValueError as error:
        raise TaskSelectionError(
            "select_task",
            f"invalid migration phase: {phase}",
        ) from error
    matches = [
        task
        for task in inspection.ordered_migration_tasks
        if task.phase == canonical_phase
        and (task_order is None or task.order == task_order)
    ]
    if len(matches) != 1:
        raise TaskSelectionError(
            "select_task",
            f"expected exactly one {phase} task, found {len(matches)}",
            diagnostics={"task_order": task_order},
        )
    return matches[0]


def resolve_task_dependencies(
    inspection: RepositoryInspection,
    task: MigrationTask,
    registry: RuleRegistry,
    *,
    max_depth: int = MAX_DEPENDENCY_DEPTH,
) -> TaskReadinessResult:
    if max_depth < 1:
        raise ValueError("max_depth must be at least 1")
    task_id = stable_task_id(task)
    references = inspection.semantic_references
    direct_usages = {
        usage.usage_id: usage for usage in inspection.discovered_gin_usages
    }
    task_usages = [
        direct_usages[usage_id]
        for usage_id in task.direct_usage_ids
        if usage_id in direct_usages
    ]
    roots = {
        (usage.file, usage.line)
        for usage in task_usages
    }
    line_references = [
        reference
        for reference in references
        if (reference.file, reference.line) in roots
    ]
    root_enclosing_symbols = {
        reference.enclosing_symbol
        for reference in line_references
        if reference.enclosing_symbol
    }
    task_files = {usage.file for usage in task_usages}
    initial = [
        reference
        for reference in references
        if (reference.file, reference.line) in roots
        or (
            reference.file in task_files
            and reference.enclosing_symbol in root_enclosing_symbols
        )
    ]
    if task_usages and not initial:
        first_usage = task_usages[0]
        initial = [
            SymbolReference(
                file=first_usage.file,
                line=first_usage.line,
                column=first_usage.column,
                symbol="<semantic dependency data unavailable>",
                resolved=False,
            )
        ]
    by_enclosing: dict[str, list[SymbolReference]] = defaultdict(list)
    for reference in references:
        if reference.enclosing_symbol:
            by_enclosing[reference.enclosing_symbol].append(reference)

    dependencies: dict[str, DependencyRef] = {}
    child_tasks: dict[str, GeneratedChildTask] = {}
    nodes: dict[str, DependencyGraphNode] = {
        task_id: DependencyGraphNode(
            node_id=task_id,
            node_type="migration_task",
            label=task_id,
        )
    }
    edges: set[tuple[str, str, str]] = set()
    cycles: set[tuple[str, ...]] = set()
    max_depth_reached = False

    def visit(
        reference: SymbolReference,
        parent_id: str,
        depth: int,
        symbol_path: tuple[str, ...],
    ) -> None:
        nonlocal max_depth_reached
        dependency = classify_reference(
            inspection,
            reference,
            registry,
            parent_id=None if parent_id == task_id else parent_id,
            depth=depth,
        )
        if (
            dependency.kind == DependencyKind.EXTERNAL_FRAMEWORK_INDEPENDENT
            and (reference.standard_library or not reference.package_path)
        ):
            return
        existing = dependencies.get(dependency.dependency_id)
        if existing is None or depth < existing.depth:
            dependencies[dependency.dependency_id] = dependency
        node_type = _node_type(dependency)
        nodes[dependency.dependency_id] = DependencyGraphNode(
            node_id=dependency.dependency_id,
            node_type=node_type,
            label=dependency.symbol,
        )
        edge_type = (
            "task_references_symbol"
            if parent_id == task_id
            else "symbol_calls_symbol"
        )
        edges.add((parent_id, dependency.dependency_id, edge_type))
        if dependency.kind.value.startswith("external_"):
            package_node = f"package:{dependency.package_path}"
            nodes[package_node] = DependencyGraphNode(
                node_id=package_node,
                node_type="external_package",
                label=dependency.package_path,
            )
            edges.add(
                (
                    dependency.dependency_id,
                    package_node,
                    "symbol_uses_package",
                )
            )

        if dependency.resolution == DependencyResolution.USE_MIGRATION_RULE:
            for rule_id in dependency.matching_rule_ids:
                rule_node = f"rule:{rule_id}"
                nodes[rule_node] = DependencyGraphNode(
                    node_id=rule_node,
                    node_type="replacement_rule",
                    label=rule_id,
                )
                edges.add(
                    (dependency.dependency_id, rule_node, "dependency_replaced_by")
                )
        if dependency.resolution == DependencyResolution.REPLACE_DEPENDENCY:
            child = _child_task(
                task_id,
                dependency,
                inspection.discovered_gin_usages,
                files=[reference.file],
            )
            child_tasks[child.task_id] = child
            nodes[child.task_id] = DependencyGraphNode(
                node_id=child.task_id,
                node_type="migration_task",
                label=child.task_id,
            )
            edges.add((task_id, child.task_id, "task_depends_on_task"))
            for rule_id in dependency.matching_rule_ids:
                rule_node = f"rule:{rule_id}"
                nodes[rule_node] = DependencyGraphNode(
                    node_id=rule_node,
                    node_type="replacement_rule",
                    label=rule_id,
                )
                edges.add(
                    (dependency.dependency_id, rule_node, "dependency_replaced_by")
                )
        if dependency.resolution != DependencyResolution.CREATE_CHILD_TASK:
            return

        child = _child_task(
            task_id,
            dependency,
            inspection.discovered_gin_usages,
            files=(
                [dependency.definition_file]
                if dependency.definition_file
                else []
            ),
        )
        child_tasks[child.task_id] = child
        nodes[child.task_id] = DependencyGraphNode(
            node_id=child.task_id,
            node_type="migration_task",
            label=child.task_id,
        )
        edges.add((task_id, child.task_id, "task_depends_on_task"))
        if dependency.symbol in symbol_path:
            start = symbol_path.index(dependency.symbol)
            cycles.add((*symbol_path[start:], dependency.symbol))
            return
        nested = by_enclosing.get(dependency.symbol, [])
        if nested and depth >= max_depth:
            max_depth_reached = True
            return
        for nested_reference in nested:
            visit(
                nested_reference,
                dependency.dependency_id,
                depth + 1,
                (*symbol_path, dependency.symbol),
            )

    for reference in initial:
        visit(reference, task_id, 1, ())

    ordered = sorted(
        dependencies.values(),
        key=lambda item: (item.depth, item.symbol, item.dependency_id),
    )
    local = [item for item in ordered if item.kind.value.startswith("local_")]
    external = [
        item for item in ordered if item.kind.value.startswith("external_")
    ]
    builtins = [
        item for item in ordered if item.kind == DependencyKind.FRAMEWORK_BUILTIN
    ]
    unresolved = [
        item for item in ordered if item.kind == DependencyKind.UNRESOLVED
    ]
    blocking = [
        item
        for item in ordered
        if item.resolution
        in {DependencyResolution.CREATE_CHILD_TASK, DependencyResolution.BLOCK}
    ]
    replacement_children = [
        item
        for item in ordered
        if item.resolution == DependencyResolution.REPLACE_DEPENDENCY
    ]
    if (
        unresolved
        or max_depth_reached
        or cycles
        or any(
            item.kind == DependencyKind.FRAMEWORK_BUILTIN
            and item.resolution == DependencyResolution.BLOCK
            for item in blocking
        )
    ):
        status = TaskReadinessStatus.BLOCKED_UNRESOLVED_DEPENDENCY
    elif any(
        item.kind == DependencyKind.EXTERNAL_FRAMEWORK_BOUND
        and item.resolution == DependencyResolution.BLOCK
        for item in blocking
    ):
        status = TaskReadinessStatus.BLOCKED_EXTERNAL_DEPENDENCY
    elif any(
        item.kind == DependencyKind.LOCAL_FRAMEWORK_BOUND for item in blocking
    ) or replacement_children:
        status = TaskReadinessStatus.BLOCKED_LOCAL_DEPENDENCIES
    else:
        status = TaskReadinessStatus.READY
    return TaskReadinessResult(
        task_id=task_id,
        ready=status == TaskReadinessStatus.READY,
        status=status,
        local_dependencies=local,
        external_dependencies=external,
        framework_builtins=builtins,
        unresolved_dependencies=unresolved,
        generated_child_tasks=sorted(
            child_tasks.values(), key=lambda child: child.task_id
        ),
        blocking_dependencies=blocking,
        dependency_graph=DependencyGraph(
            nodes=sorted(nodes.values(), key=lambda node: node.node_id),
            edges=[
                DependencyGraphEdge(source=source, target=target, edge_type=kind)
                for source, target, kind in sorted(edges)
            ],
            cycles=[list(cycle) for cycle in sorted(cycles)],
            max_depth_reached=max_depth_reached,
        ),
    )


def classify_reference(
    inspection: RepositoryInspection,
    reference: SymbolReference,
    registry: RuleRegistry,
    *,
    parent_id: str | None,
    depth: int,
) -> DependencyRef:
    rule = registry.lookup_resolved_symbol(reference.symbol)
    rule_ids = [rule.rule_id] if rule is not None else []
    replacement = rule.target_package if rule is not None else None
    if not reference.resolved:
        kind = DependencyKind.UNRESOLVED
        resolution = DependencyResolution.BLOCK
        notes = (
            "migration plan has no semantic references for this task; "
            "regenerate the plan with the current Go analyzer"
            if reference.symbol == "<semantic dependency data unavailable>"
            else "symbol could not be resolved by Go type information"
        )
    elif reference.package_path == GIN_PACKAGE:
        kind = DependencyKind.FRAMEWORK_BUILTIN
        resolution = (
            DependencyResolution.USE_MIGRATION_RULE
            if rule is not None
            else DependencyResolution.BLOCK
        )
        notes = "Gin framework builtin"
    elif _is_local(inspection.repository_summary.module_path, reference.package_path):
        kind = (
            DependencyKind.LOCAL_FRAMEWORK_BOUND
            if reference.framework_bound
            else DependencyKind.LOCAL_FRAMEWORK_INDEPENDENT
        )
        resolution = (
            DependencyResolution.CREATE_CHILD_TASK
            if reference.framework_bound
            else DependencyResolution.NO_ACTION
        )
        notes = (
            "repository-local symbol is coupled to Gin"
            if reference.framework_bound
            else "repository-local symbol is framework-independent"
        )
    elif reference.standard_library or not reference.package_path:
        kind = DependencyKind.EXTERNAL_FRAMEWORK_INDEPENDENT
        resolution = DependencyResolution.NO_ACTION
        notes = "standard library or language builtin"
    elif reference.framework_bound:
        kind = DependencyKind.EXTERNAL_FRAMEWORK_BOUND
        resolution = (
            DependencyResolution.REPLACE_DEPENDENCY
            if rule is not None
            and rule.resolution_strategy
            == ResolutionStrategy.REPLACE_DEPENDENCY
            else DependencyResolution.BLOCK
        )
        notes = (
            "deterministic dependency replacement is available"
            if resolution == DependencyResolution.REPLACE_DEPENDENCY
            else "no deterministic dependency replacement rule"
        )
    else:
        kind = DependencyKind.EXTERNAL_FRAMEWORK_INDEPENDENT
        resolution = DependencyResolution.NO_ACTION
        notes = "external package is framework-independent"
    return DependencyRef(
        dependency_id=_dependency_id(reference.symbol),
        symbol=reference.symbol,
        package_path=reference.package_path,
        module_path=reference.module_path,
        definition_file=reference.definition_file or None,
        definition_line=reference.definition_line or None,
        kind=kind,
        resolution=resolution,
        framework_bound=reference.framework_bound,
        matching_rule_ids=rule_ids,
        replacement_package=replacement,
        parent_dependency_id=parent_id,
        depth=depth,
        notes=notes,
    )


def _child_task(
    parent_task_id: str,
    dependency: DependencyRef,
    task_usages,
    *,
    files: list[str],
) -> GeneratedChildTask:
    phase = (
        MigrationPhase.DEPENDENCIES
        if dependency.resolution == DependencyResolution.REPLACE_DEPENDENCY
        else _local_phase(dependency)
    )
    task_id = "dependency-task-" + hashlib.sha256(
        f"{parent_task_id}\0{phase.value}\0{dependency.symbol}".encode()
    ).hexdigest()[:16]
    usage_ids = sorted(
        usage.usage_id
        for usage in task_usages
        if dependency.definition_file == usage.file
    )
    return GeneratedChildTask(
        task_id=task_id,
        parent_task_id=parent_task_id,
        phase=phase,
        source_symbol=dependency.symbol,
        files=sorted(set(files)),
        direct_usage_ids=usage_ids,
        depends_on=[],
        reason=dependency.notes,
        status="pending",
        depth=dependency.depth,
    )


def _local_phase(dependency: DependencyRef) -> MigrationPhase:
    lowered = f"{dependency.symbol} {dependency.definition_file or ''}".casefold()
    if "middleware" in lowered or "jwt" in lowered:
        return MigrationPhase.MIDDLEWARE
    return MigrationPhase.HANDLER_SIGNATURES


def _dependency_id(symbol: str) -> str:
    return "dependency-" + hashlib.sha256(symbol.encode()).hexdigest()[:16]


def _is_local(module_path: str, package_path: str) -> bool:
    return bool(module_path) and (
        package_path == module_path or package_path.startswith(f"{module_path}/")
    )


def _node_type(dependency: DependencyRef) -> str:
    if dependency.kind.value.startswith("local_"):
        return "local_symbol"
    if dependency.kind.value.startswith("external_"):
        return "external_package"
    if dependency.kind == DependencyKind.FRAMEWORK_BUILTIN:
        return "framework_builtin"
    return "unresolved_symbol"
