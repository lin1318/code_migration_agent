"""Public service for loading inputs and executing one migration task."""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import ValidationError

from agent.execution.errors import TaskExecutionError
from agent.execution.executor import execute_task
from agent.execution.models import TaskExecutionResult
from models.dependencies import TaskReadinessResult
from knowledge.registry import RuleRegistryError, load_rule_registry
from llm.base import ModelClient
from models.inspection import RepositoryInspection
from models.workflow import WorkflowReport
from tools.inspection import DEFAULT_RULE_PATH

MAX_PLAN_SIZE = 20 * 1024 * 1024


def execute_migration_task(
    repository: str | os.PathLike[str],
    plan_path: str | os.PathLike[str],
    model_client: ModelClient,
    *,
    phase: str,
    task_order: int | None = None,
    rule_path: str | os.PathLike[str] = DEFAULT_RULE_PATH,
    completed_phases: list[str] | None = None,
    updated_source_content: dict[str, str] | None = None,
) -> TaskExecutionResult | TaskReadinessResult:
    plan = _load_plan(plan_path)
    try:
        registry = load_rule_registry(rule_path)
    except RuleRegistryError as error:
        raise TaskExecutionError(
            "load_migration_rules",
            str(error),
        ) from error
    return execute_task(
        str(repository),
        plan,
        registry,
        model_client,
        phase=phase,
        task_order=task_order,
        completed_phases=completed_phases,
        updated_source_content=updated_source_content,
    )


def _load_plan(path: str | os.PathLike[str]) -> RepositoryInspection:
    candidate = Path(path).expanduser()
    if not candidate.is_file():
        raise TaskExecutionError(
            "load_plan",
            f"migration plan does not exist or is not a file: {candidate}",
        )
    if candidate.stat().st_size > MAX_PLAN_SIZE:
        raise TaskExecutionError(
            "load_plan",
            f"migration plan exceeds {MAX_PLAN_SIZE} bytes: {candidate}",
        )
    try:
        document = candidate.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise TaskExecutionError(
            "load_plan",
            f"invalid migration plan JSON: {error}",
        ) from error
    try:
        return RepositoryInspection.model_validate_json(document)
    except ValidationError as inspection_error:
        try:
            workflow_report = WorkflowReport.model_validate_json(document)
            return RepositoryInspection.model_validate(
                {
                    field: getattr(workflow_report, field)
                    for field in RepositoryInspection.model_fields
                }
            )
        except ValidationError as workflow_error:
            raise TaskExecutionError(
                "load_plan",
                "invalid migration plan JSON: expected an inspect result or "
                f"successful workflow report: {inspection_error}; "
                f"{workflow_error}",
            ) from workflow_error
