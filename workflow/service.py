"""Public service for invoking the deterministic inspection workflow."""

from __future__ import annotations

from os import PathLike

from langgraph.checkpoint.base import BaseCheckpointSaver

from models.workflow import WorkflowState
from tools.inspection import DEFAULT_RULE_PATH
from workflow.graph import create_inspection_workflow


def run_inspection_workflow(
    repository_path: str | PathLike[str],
    *,
    rule_registry_path: str | PathLike[str] = DEFAULT_RULE_PATH,
    analyzer_timeout: float = 30.0,
    checkpointer: BaseCheckpointSaver | None = None,
    thread_id: str = "workflow-inspect",
) -> WorkflowState:
    graph = create_inspection_workflow(checkpointer=checkpointer)
    config = (
        {"configurable": {"thread_id": thread_id}}
        if checkpointer is not None
        else None
    )
    return graph.invoke(
        {
            "repository_path": str(repository_path),
            "rule_registry_path": str(rule_registry_path),
            "analyzer_timeout": analyzer_timeout,
        },
        config=config,
    )
