"""LangGraph construction for the file-oriented migration workflow."""

from __future__ import annotations

from functools import partial
from typing import Literal

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from agent.file_bundle_execution import FileBundleModelClient
from models.file_migration_execution import FileMigrationRunStatus
from models.file_migration_workflow import FileMigrationGraphState
from workflow import file_nodes

_PRE_EXECUTION_STAGES = (
    ("validate_input", file_nodes.validate_input),
    ("inspect_repository", file_nodes.inspect),
    ("load_migration_rules", file_nodes.load_rules),
    ("classify_files", file_nodes.classify_files),
    ("build_file_plan", file_nodes.build_file_plan),
    ("build_bundle_plan", file_nodes.build_bundle_plan),
    ("prepare_output", file_nodes.prepare_output),
)


def create_file_migration_workflow(
    model_client: FileBundleModelClient,
    *,
    checkpointer: BaseCheckpointSaver | None = None,
):
    builder = StateGraph(FileMigrationGraphState)
    for name, node in _PRE_EXECUTION_STAGES:
        builder.add_node(name, node)
    builder.add_node(
        "execute_bundles",
        partial(file_nodes.execute_bundles, model_client=model_client),
    )
    builder.add_node("validate_repository", file_nodes.validate_repository)
    builder.add_node("generate_report", file_nodes.generate_report)
    builder.add_node("handle_failure", file_nodes.handle_failure)
    builder.add_edge(START, _PRE_EXECUTION_STAGES[0][0])
    names = [name for name, _node in _PRE_EXECUTION_STAGES]
    for current, following in zip(names, names[1:]):
        builder.add_conditional_edges(
            current,
            _route_error,
            {"continue": following, "failure": "handle_failure"},
        )
    builder.add_conditional_edges(
        names[-1],
        _route_error,
        {"continue": "execute_bundles", "failure": "handle_failure"},
    )
    builder.add_conditional_edges(
        "execute_bundles",
        _route_after_execution,
        {
            "validate": "validate_repository",
            "report": "generate_report",
            "failure": "handle_failure",
        },
    )
    builder.add_conditional_edges(
        "validate_repository",
        _route_error,
        {"continue": "generate_report", "failure": "handle_failure"},
    )
    builder.add_conditional_edges(
        "generate_report",
        _route_error,
        {"continue": END, "failure": "handle_failure"},
    )
    builder.add_edge("handle_failure", END)
    return builder.compile(checkpointer=checkpointer)


def _route_error(
    state: FileMigrationGraphState,
) -> Literal["continue", "failure"]:
    return "failure" if state.get("errors") else "continue"


def _route_after_execution(
    state: FileMigrationGraphState,
) -> Literal["validate", "report", "failure"]:
    if state.get("errors"):
        return "failure"
    execution = state.get("execution")
    if execution is None:
        return "failure"
    return (
        "validate"
        if execution.status is FileMigrationRunStatus.COMPLETED
        else "report"
    )
