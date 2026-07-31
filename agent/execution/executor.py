"""Orchestrate exactly one migration task proposal."""

from __future__ import annotations

from agent.execution.context_builder import (
    build_task_context,
    select_task,
    stable_task_id,
)
from agent.execution.models import ContextSummary, TaskExecutionResult
from agent.execution.prompt_builder import build_prompt
from agent.execution.validator import validate_proposal
from agent.dependency_resolver import (
    MAX_DEPENDENCY_DEPTH,
    resolve_task_dependencies,
)
from knowledge.registry import RuleRegistry
from llm.base import ModelClient
from models.inspection import RepositoryInspection
from models.dependencies import TaskReadinessResult


def execute_task(
    repository: str,
    inspection: RepositoryInspection,
    registry: RuleRegistry,
    model_client: ModelClient,
    *,
    phase: str,
    task_order: int | None = None,
    completed_phases: list[str] | None = None,
    updated_source_content: dict[str, str] | None = None,
) -> TaskExecutionResult | TaskReadinessResult:
    task = select_task(
        inspection,
        phase=phase,
        task_order=task_order,
    )
    readiness = resolve_task_dependencies(
        inspection,
        task,
        registry,
        max_depth=MAX_DEPENDENCY_DEPTH,
    )
    if not readiness.ready:
        return readiness
    context = build_task_context(
        repository,
        inspection,
        task,
        registry,
        completed_phases=completed_phases,
        updated_source_content=updated_source_content,
    )
    prompt = build_prompt(context)
    proposal = model_client.generate_patch(context, prompt)
    validate_proposal(
        proposal,
        expected_task_id=stable_task_id(task),
        allowed_files=set(context.allowed_files),
        context=context,
    )
    return TaskExecutionResult(
        selected_task=task,
        context_summary=ContextSummary(
            allowed_file_count=len(context.allowed_files),
            direct_usage_count=len(context.direct_usages),
            migration_rule_count=len(context.matching_migration_rules),
            source_file_count=len(context.relevant_source_files),
            line_range_count=len(context.relevant_line_ranges),
            related_symbol_count=len(context.related_symbol_definitions),
        ),
        prompt_size=len(prompt),
        model_used=model_client.model_name,
        proposal=proposal,
    )
