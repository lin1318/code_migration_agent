"""Typed dependency-resolution and task-readiness models."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from models.migration_plan import MigrationPhase


class DependencyModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DependencyKind(StrEnum):
    LOCAL_FRAMEWORK_BOUND = "local_framework_bound"
    LOCAL_FRAMEWORK_INDEPENDENT = "local_framework_independent"
    EXTERNAL_FRAMEWORK_BOUND = "external_framework_bound"
    EXTERNAL_FRAMEWORK_INDEPENDENT = "external_framework_independent"
    FRAMEWORK_BUILTIN = "framework_builtin"
    UNRESOLVED = "unresolved"


class DependencyResolution(StrEnum):
    NO_ACTION = "no_action"
    CREATE_CHILD_TASK = "create_child_task"
    USE_MIGRATION_RULE = "use_migration_rule"
    REPLACE_DEPENDENCY = "replace_dependency"
    BLOCK = "block"


class TaskReadinessStatus(StrEnum):
    READY = "ready"
    BLOCKED_LOCAL_DEPENDENCIES = "blocked_local_dependencies"
    BLOCKED_EXTERNAL_DEPENDENCY = "blocked_external_dependency"
    BLOCKED_UNRESOLVED_DEPENDENCY = "blocked_unresolved_dependency"


class DependencyRef(DependencyModel):
    dependency_id: str
    symbol: str
    package_path: str
    module_path: str
    definition_file: str | None
    definition_line: int | None = Field(default=None, ge=1)
    kind: DependencyKind
    resolution: DependencyResolution
    framework_bound: bool
    matching_rule_ids: list[str]
    replacement_package: str | None
    parent_dependency_id: str | None
    depth: int = Field(ge=0)
    notes: str


class GeneratedChildTask(DependencyModel):
    task_id: str
    parent_task_id: str
    phase: MigrationPhase
    source_symbol: str
    files: list[str]
    direct_usage_ids: list[str]
    depends_on: list[str]
    reason: str
    status: str
    depth: int = Field(ge=1)


class DependencyGraphNode(DependencyModel):
    node_id: str
    node_type: str
    label: str


class DependencyGraphEdge(DependencyModel):
    source: str
    target: str
    edge_type: str


class DependencyGraph(DependencyModel):
    nodes: list[DependencyGraphNode]
    edges: list[DependencyGraphEdge]
    cycles: list[list[str]]
    max_depth_reached: bool


class TaskReadinessResult(DependencyModel):
    task_id: str
    ready: bool
    status: TaskReadinessStatus
    local_dependencies: list[DependencyRef]
    external_dependencies: list[DependencyRef]
    framework_builtins: list[DependencyRef]
    unresolved_dependencies: list[DependencyRef]
    generated_child_tasks: list[GeneratedChildTask]
    blocking_dependencies: list[DependencyRef]
    dependency_graph: DependencyGraph
