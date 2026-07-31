"""Deterministic repository-wide migration dependency and bundle planning."""

from __future__ import annotations

import hashlib
from collections import defaultdict
from pathlib import PurePosixPath

from agent.dependency_resolver import classify_reference
from knowledge.registry import RuleRegistry
from models.bundles import (
    BundleComplexity,
    BundleDependencyGraph,
    BundleGraphEdge,
    MigrationBundle,
    RepositoryDependencyGraph,
    RepositoryGraphEdge,
    RepositoryGraphNode,
    RepositoryMigrationJob,
    RepositoryMigrationPlan,
)
from models.dependencies import DependencyKind, DependencyResolution
from models.inspection import RepositoryInspection


def build_repository_migration_plan(
    inspection: RepositoryInspection,
    registry: RuleRegistry,
) -> RepositoryMigrationPlan:
    framework_usages = [
        usage
        for usage in inspection.discovered_gin_usages
        if usage.kind != "gin_import"
    ]
    framework_files = {usage.file for usage in framework_usages}
    local_edges: set[tuple[str, str]] = set()
    external_by_file = defaultdict(dict)
    nodes: dict[str, RepositoryGraphNode] = {}
    graph_edges: set[tuple[str, str, str]] = set()

    for usage in framework_usages:
        file_node = _file_node(usage.file)
        symbol_node = _symbol_node(usage.symbol)
        nodes[file_node.node_id] = file_node
        nodes[symbol_node.node_id] = symbol_node
        graph_edges.add((file_node.node_id, symbol_node.node_id, "framework_usage"))

    for reference in inspection.semantic_references:
        if not reference.framework_bound:
            continue
        source_file = reference.file
        framework_files.add(source_file)
        source_node = _file_node(source_file)
        symbol_node = _symbol_node(reference.symbol)
        nodes[source_node.node_id] = source_node
        nodes[symbol_node.node_id] = symbol_node
        edge_type = _reference_edge_type(reference.symbol)
        graph_edges.add((source_node.node_id, symbol_node.node_id, edge_type))
        dependency = classify_reference(
            inspection,
            reference,
            registry,
            parent_id=None,
            depth=1,
        )
        if dependency.kind == DependencyKind.LOCAL_FRAMEWORK_BOUND:
            if dependency.definition_file:
                target = dependency.definition_file
                framework_files.add(target)
                nodes[_file_node(target).node_id] = _file_node(target)
                graph_edges.add(
                    (
                        symbol_node.node_id,
                        _file_node(target).node_id,
                        "symbol_definition",
                    )
                )
                if source_file != target:
                    local_edges.add((source_file, target))
        elif dependency.kind == DependencyKind.EXTERNAL_FRAMEWORK_BOUND:
            external_by_file[source_file][dependency.dependency_id] = dependency
            package_id = f"external:{dependency.package_path}"
            nodes[package_id] = RepositoryGraphNode(
                node_id=package_id,
                node_type="external_package",
                label=dependency.package_path,
            )
            graph_edges.add((symbol_node.node_id, package_id, "symbol_uses_package"))
        elif dependency.kind == DependencyKind.FRAMEWORK_BUILTIN:
            rule_ids = dependency.matching_rule_ids
            concern_id = f"concern:{rule_ids[0] if rule_ids else reference.symbol}"
            nodes[concern_id] = RepositoryGraphNode(
                node_id=concern_id,
                node_type="migration_concern",
                label=rule_ids[0] if rule_ids else reference.symbol,
            )
            graph_edges.add((symbol_node.node_id, concern_id, "framework_usage"))

    groups = _file_groups(framework_files, local_edges)
    bundle_id_by_file: dict[str, str] = {}
    bundles: list[MigrationBundle] = []
    usages_by_file = defaultdict(list)
    for usage in framework_usages:
        usages_by_file[usage.file].append(usage)
    for files in groups:
        bundle_id = _bundle_id(files)
        for file in files:
            bundle_id_by_file[file] = bundle_id
        symbols = sorted(
            {
                usage.symbol
                for file in files
                for usage in usages_by_file[file]
            }
            | {
                reference.symbol
                for reference in inspection.semantic_references
                if reference.framework_bound and reference.file in files
            }
        )
        concerns = sorted(
            {
                _usage_concern(usage.kind)
                for file in files
                for usage in usages_by_file[file]
            }
        )
        if symbols and not concerns:
            concerns = ["framework_usage"]
        external = sorted(
            {
                dependency.dependency_id: dependency
                for file in files
                for dependency in external_by_file[file].values()
            }.values(),
            key=lambda item: item.dependency_id,
        )
        blockers = [
            dependency
            for dependency in external
            if dependency.resolution == DependencyResolution.BLOCK
        ]
        bundles.append(
            MigrationBundle(
                bundle_id=bundle_id,
                files=files,
                framework_symbols=symbols,
                migration_concerns=concerns,
                external_dependencies=external,
                blocking_dependencies=blockers,
                depends_on=[],
                estimated_complexity=_complexity(files, symbols),
            )
        )

    bundle_edges = {
        (bundle_id_by_file[source], bundle_id_by_file[target])
        for source, target in local_edges
        if bundle_id_by_file[source] != bundle_id_by_file[target]
    }
    dependencies = defaultdict(set)
    for source, target in bundle_edges:
        dependencies[source].add(target)
    bundles = [
        bundle.model_copy(
            update={"depends_on": sorted(dependencies[bundle.bundle_id])}
        )
        for bundle in sorted(bundles, key=lambda item: item.bundle_id)
    ]
    bundle_cycles = _cycles(
        {bundle.bundle_id for bundle in bundles},
        bundle_edges,
    )
    all_ordering = _dependency_first_order(
        {bundle.bundle_id for bundle in bundles},
        bundle_edges,
    )
    unavailable = {
        bundle.bundle_id for bundle in bundles if bundle.blocking_dependencies
    }
    changed = True
    while changed:
        changed = False
        for source, target in bundle_edges:
            if target in unavailable and source not in unavailable:
                unavailable.add(source)
                changed = True
    ordering = [
        bundle_id for bundle_id in all_ordering if bundle_id not in unavailable
    ]
    reachable_files = _reachable_files(framework_files, local_edges)
    unreachable = sorted(
        _file_node(file).node_id for file in framework_files - reachable_files
    )
    job_key = "\0".join(
        [inspection.repository_summary.module_path, *sorted(framework_files)]
    )
    return RepositoryMigrationPlan(
        repository_job=RepositoryMigrationJob(
            job_id="migration-job-" + _hash(job_key),
            repository_path=inspection.repository_path,
            module_path=inspection.repository_summary.module_path,
            framework="github.com/gin-gonic/gin",
            target_framework="github.com/cloudwego/hertz",
        ),
        dependency_graph=RepositoryDependencyGraph(
            nodes=sorted(nodes.values(), key=lambda node: node.node_id),
            edges=[
                RepositoryGraphEdge(source=source, target=target, edge_type=kind)
                for source, target, kind in sorted(graph_edges)
            ],
            cycles=[list(cycle) for cycle in _cycles(framework_files, local_edges)],
            unreachable_node_ids=unreachable,
        ),
        bundles=bundles,
        bundle_graph=BundleDependencyGraph(
            nodes=sorted(bundle.bundle_id for bundle in bundles),
            edges=[
                BundleGraphEdge(
                    source_bundle_id=source,
                    target_bundle_id=target,
                )
                for source, target in sorted(bundle_edges)
            ],
            cycles=[list(cycle) for cycle in bundle_cycles],
        ),
        ready_bundle_ordering=ordering,
    )


def _file_groups(
    files: set[str],
    edges: set[tuple[str, str]],
) -> list[list[str]]:
    parent = {file: file for file in files}

    def find(file: str) -> str:
        while parent[file] != file:
            parent[file] = parent[parent[file]]
            file = parent[file]
        return file

    def union(left: str, right: str) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[max(left_root, right_root)] = min(left_root, right_root)

    by_directory = defaultdict(list)
    for file in files:
        by_directory[PurePosixPath(file).parent.as_posix()].append(file)
    for directory_files in by_directory.values():
        for file in directory_files[1:]:
            union(directory_files[0], file)
    for cycle in _cycles(files, edges):
        for file in cycle[1:]:
            union(cycle[0], file)
    groups = defaultdict(list)
    for file in sorted(files):
        groups[find(file)].append(file)
    return sorted(groups.values(), key=lambda group: tuple(group))


def _cycles(
    nodes: set[str],
    edges: set[tuple[str, str]],
) -> list[tuple[str, ...]]:
    adjacency = defaultdict(list)
    for source, target in sorted(edges):
        adjacency[source].append(target)
    found: set[tuple[str, ...]] = set()

    def visit(node: str, path: tuple[str, ...]) -> None:
        if node in path:
            cycle = (*path[path.index(node) :], node)
            rotations = [cycle[index:-1] + cycle[:index] for index in range(len(cycle) - 1)]
            canonical = min(rotations)
            found.add((*canonical, canonical[0]))
            return
        for target in adjacency[node]:
            visit(target, (*path, node))

    for node in sorted(nodes):
        visit(node, ())
    return sorted(found)


def _dependency_first_order(
    nodes: set[str],
    edges: set[tuple[str, str]],
) -> list[str]:
    dependencies = defaultdict(set)
    for source, target in edges:
        dependencies[source].add(target)
    remaining = set(nodes)
    ordered: list[str] = []
    while remaining:
        ready = sorted(
            node for node in remaining if not (dependencies[node] & remaining)
        )
        if not ready:
            ready = [min(remaining)]
        ordered.extend(ready)
        remaining.difference_update(ready)
    return ordered


def _reachable_files(
    files: set[str],
    edges: set[tuple[str, str]],
) -> set[str]:
    incoming = {target for _, target in edges}
    roots = sorted(files - incoming)
    adjacency = defaultdict(list)
    for source, target in edges:
        adjacency[source].append(target)
    reached: set[str] = set()
    stack = list(roots)
    while stack:
        current = stack.pop()
        if current in reached:
            continue
        reached.add(current)
        stack.extend(adjacency[current])
    return reached


def _file_node(file: str) -> RepositoryGraphNode:
    return RepositoryGraphNode(
        node_id=f"file:{file}",
        node_type="source_file",
        label=file,
        file=file,
    )


def _symbol_node(symbol: str) -> RepositoryGraphNode:
    return RepositoryGraphNode(
        node_id=f"symbol:{symbol}",
        node_type="framework_symbol",
        label=symbol,
    )


def _reference_edge_type(symbol: str) -> str:
    lowered = symbol.casefold()
    if "middleware" in lowered or ".jwt" in lowered:
        return "middleware_reference"
    if ".(*engine)." in lowered or ".(*routergroup)." in lowered:
        return "framework_usage"
    return "handler_reference"


def _usage_concern(kind: str) -> str:
    return {
        "constructor": "server_bootstrap",
        "engine_method": "routing",
        "router_group_method": "routing",
        "group": "routing",
        "middleware": "middleware",
        "gin_type": "handler_signatures",
        "context_method": "context_operations",
        "gin_h": "responses",
    }.get(kind, "framework_usage")


def _complexity(files: list[str], symbols: list[str]) -> BundleComplexity:
    score = len(files) * 2 + len(symbols)
    if score <= 6:
        return BundleComplexity.LOW
    if score <= 18:
        return BundleComplexity.MEDIUM
    return BundleComplexity.HIGH


def _bundle_id(files: list[str]) -> str:
    return "migration-bundle-" + _hash("\0".join(files))


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16]
