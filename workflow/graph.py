"""StateGraph construction for deterministic repository inspection."""

from __future__ import annotations

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph

from models.workflow import WorkflowState
from workflow import nodes
from workflow.routes import route_after_stage

_STAGES = (
    "validate_input",
    "inspect_repository",
    "load_migration_rules",
    "build_inventory",
    "validate_inventory",
    "build_plan",
    "validate_plan",
    "generate_report",
)


def create_inspection_workflow(
    checkpointer: BaseCheckpointSaver | None = None,
):
    """Compile the inspection graph, optionally with a test memory saver."""

    builder = StateGraph(WorkflowState)
    for stage in _STAGES:
        builder.add_node(stage, getattr(nodes, stage))
    builder.add_node("handle_failure", nodes.handle_failure)
    builder.add_edge(START, _STAGES[0])
    for current, following in zip(_STAGES, (*_STAGES[1:], END)):
        builder.add_conditional_edges(
            current,
            route_after_stage,
            {
                "continue": following,
                "failure": "handle_failure",
            },
        )
    builder.add_edge("handle_failure", END)
    return builder.compile(checkpointer=checkpointer)
