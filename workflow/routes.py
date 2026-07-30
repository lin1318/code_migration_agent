"""Conditional routing functions for the inspection workflow."""

from typing import Literal

from models.workflow import WorkflowState


def route_after_stage(state: WorkflowState) -> Literal["continue", "failure"]:
    return "failure" if state.get("errors") else "continue"
