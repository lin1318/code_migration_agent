"""Public invocation service for LangGraph-managed file migration."""

from __future__ import annotations

from os import PathLike

from langgraph.checkpoint.base import BaseCheckpointSaver

from agent.file_bundle_execution import FileBundleModelClient
from models.file_migration_workflow import FileMigrationGraphState
from workflow.file_graph import create_file_migration_workflow


def run_file_migration_workflow(
    repository_path: str | PathLike[str],
    output_directory: str | PathLike[str],
    rule_registry_path: str | PathLike[str],
    model_client: FileBundleModelClient,
    *,
    analyzer_timeout: float,
    apply_timeout: float,
    validation_timeout: float,
    allow_dirty: bool,
    checkpointer: BaseCheckpointSaver | None = None,
    thread_id: str = "workflow-migrate-files",
) -> FileMigrationGraphState:
    graph = create_file_migration_workflow(
        model_client,
        checkpointer=checkpointer,
    )
    config = (
        {"configurable": {"thread_id": thread_id}}
        if checkpointer is not None
        else None
    )
    return graph.invoke(
        {
            "repository_path": str(repository_path),
            "output_directory": str(output_directory),
            "rule_registry_path": str(rule_registry_path),
            "analyzer_timeout": analyzer_timeout,
            "apply_timeout": apply_timeout,
            "validation_timeout": validation_timeout,
            "allow_dirty": allow_dirty,
        },
        config=config,
    )
