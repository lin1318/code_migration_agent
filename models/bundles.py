"""Repository-wide deterministic migration bundle models."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

from models.dependencies import DependencyRef


class BundleModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BundleComplexity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class RepositoryMigrationJob(BundleModel):
    job_id: str
    repository_path: str
    module_path: str
    framework: str
    target_framework: str


class RepositoryGraphNode(BundleModel):
    node_id: str
    node_type: str
    label: str
    file: str | None = None


class RepositoryGraphEdge(BundleModel):
    source: str
    target: str
    edge_type: str


class RepositoryDependencyGraph(BundleModel):
    nodes: list[RepositoryGraphNode]
    edges: list[RepositoryGraphEdge]
    cycles: list[list[str]]
    unreachable_node_ids: list[str]


class MigrationBundle(BundleModel):
    bundle_id: str
    files: list[str]
    framework_symbols: list[str]
    migration_concerns: list[str]
    external_dependencies: list[DependencyRef]
    blocking_dependencies: list[DependencyRef]
    depends_on: list[str]
    estimated_complexity: BundleComplexity


class BundleGraphEdge(BundleModel):
    source_bundle_id: str
    target_bundle_id: str


class BundleDependencyGraph(BundleModel):
    nodes: list[str]
    edges: list[BundleGraphEdge]
    cycles: list[list[str]]


class RepositoryMigrationPlan(BundleModel):
    repository_job: RepositoryMigrationJob
    dependency_graph: RepositoryDependencyGraph
    bundles: list[MigrationBundle]
    bundle_graph: BundleDependencyGraph
    ready_bundle_ordering: list[str]
