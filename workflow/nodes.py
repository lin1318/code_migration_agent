"""Thin LangGraph nodes that compose existing deterministic services."""

from __future__ import annotations

from pathlib import Path

from knowledge.registry import RuleRegistry, RuleRegistryError, load_rule_registry
from models.inspection import RepositoryInspection
from models.workflow import (
    RuleRegistryMetadata,
    WorkflowError,
    WorkflowFailureReport,
    WorkflowReport,
    WorkflowState,
)
from tools.go_analyzer import AnalyzerError, run_go_analyzer
from tools.inspection import (
    DEFAULT_ANALYZER_COMMAND,
    DEFAULT_ANALYZER_DIRECTORY,
    packages_containing_gin_usages,
)
from tools.migration_planner import (
    build_migration_inventory,
    get_ordered_migration_plan,
    validate_inventory_classification,
    validate_migration_plan,
)


def validate_input(state: WorkflowState) -> dict:
    stage = "validate_input"
    updates = {
        "current_stage": stage,
        "completed_stages": [],
        "analyzer_result": None,
        "migration_rules": [],
        "rule_registry_metadata": None,
        "migration_inventory": None,
        "migration_plan": None,
        "repository_summary": None,
        "warnings": [],
        "errors": [],
        "final_report": None,
        "final_status": None,
    }
    repository = Path(state["repository_path"]).expanduser()
    rules = Path(state["rule_registry_path"]).expanduser()
    timeout = state["analyzer_timeout"]
    error: ValueError | None = None
    if not repository.exists():
        error = ValueError(f"repository path does not exist: {repository}")
    elif not repository.is_dir():
        error = ValueError(f"repository path is not a directory: {repository}")
    elif not rules.exists():
        error = ValueError(f"rule registry path does not exist: {rules}")
    elif not (rules.is_dir() or rules.is_file()):
        error = ValueError(f"rule registry path is invalid: {rules}")
    elif timeout <= 0:
        error = ValueError("analyzer timeout must be greater than zero")
    if error is not None:
        updates["errors"] = [_workflow_error(stage, error)]
        return updates
    updates.update(
        {
            "repository_path": str(repository.resolve()),
            "rule_registry_path": str(rules.resolve()),
            "completed_stages": [stage],
        }
    )
    return updates


def inspect_repository(state: WorkflowState) -> dict:
    stage = "inspect_repository"
    try:
        execution = run_go_analyzer(
            state["repository_path"],
            analyzer_command=DEFAULT_ANALYZER_COMMAND,
            timeout=state["analyzer_timeout"],
            working_directory=DEFAULT_ANALYZER_DIRECTORY,
        )
    except AnalyzerError as error:
        return _failure_update(state, stage, error)
    return {
        "current_stage": stage,
        "completed_stages": _completed(state, stage),
        "analyzer_result": execution.result,
    }


def load_migration_rules(state: WorkflowState) -> dict:
    stage = "load_migration_rules"
    try:
        registry = load_rule_registry(state["rule_registry_path"])
    except RuleRegistryError as error:
        return _failure_update(state, stage, error)
    rules = list(registry.rules)
    return {
        "current_stage": stage,
        "completed_stages": _completed(state, stage),
        "migration_rules": rules,
        "rule_registry_metadata": RuleRegistryMetadata(
            path=state["rule_registry_path"],
            rule_count=len(rules),
            rule_ids=sorted(rule.rule_id for rule in rules),
        ),
    }


def build_inventory(state: WorkflowState) -> dict:
    stage = "build_inventory"
    try:
        registry = RuleRegistry(state["migration_rules"])
        inventory = build_migration_inventory(state["analyzer_result"], registry)
    except (RuleRegistryError, ValueError) as error:
        return _failure_update(state, stage, error)
    warnings = list(state["warnings"])
    if inventory.unsupported_usages:
        warnings.append(
            f"{len(inventory.unsupported_usages)} Gin usages are unsupported"
        )
    if inventory.unmatched_usages:
        warnings.append(
            f"{len(inventory.unmatched_usages)} Gin usages have no migration rule"
        )
    return {
        "current_stage": stage,
        "completed_stages": _completed(state, stage),
        "migration_inventory": inventory,
        "repository_summary": inventory.repository_summary,
        "warnings": warnings,
    }


def validate_inventory(state: WorkflowState) -> dict:
    stage = "validate_inventory"
    try:
        validate_inventory_classification(state["migration_inventory"])
    except ValueError as error:
        return _failure_update(state, stage, error)
    return {
        "current_stage": stage,
        "completed_stages": _completed(state, stage),
    }


def build_plan(state: WorkflowState) -> dict:
    stage = "build_plan"
    try:
        plan = get_ordered_migration_plan(state["migration_inventory"])
    except ValueError as error:
        return _failure_update(state, stage, error)
    return {
        "current_stage": stage,
        "completed_stages": _completed(state, stage),
        "migration_plan": plan,
    }


def validate_plan(state: WorkflowState) -> dict:
    stage = "validate_plan"
    try:
        validate_migration_plan(
            state["migration_plan"],
            state["migration_inventory"].discovered_gin_usages,
        )
    except ValueError as error:
        return _failure_update(state, stage, error)
    return {
        "current_stage": stage,
        "completed_stages": _completed(state, stage),
    }


def generate_report(state: WorkflowState) -> dict:
    stage = "generate_report"
    try:
        inventory = state["migration_inventory"]
        inspection = RepositoryInspection.from_inventory(
            state["repository_path"],
            packages_containing_gin_usages(state["analyzer_result"]),
            inventory.model_copy(update={"tasks": state["migration_plan"]}),
            state["analyzer_result"].references,
        )
        completed = _completed(state, stage)
        report = WorkflowReport(
            **inspection.model_dump(),
            completed_stages=completed,
            warnings=state["warnings"],
            errors=[],
            final_status="success",
        )
    except ValueError as error:
        return _failure_update(state, stage, error)
    return {
        "current_stage": stage,
        "completed_stages": completed,
        "final_report": report,
        "final_status": "success",
    }


def handle_failure(state: WorkflowState) -> dict:
    stage = "handle_failure"
    report = WorkflowFailureReport(
        repository_path=state["repository_path"],
        completed_stages=state.get("completed_stages", []),
        warnings=state.get("warnings", []),
        errors=state.get("errors", []),
        final_status="failed",
    )
    return {
        "current_stage": stage,
        "final_report": report,
        "final_status": "failed",
    }


def _completed(state: WorkflowState, stage: str) -> list[str]:
    return [*state.get("completed_stages", []), stage]


def _failure_update(
    state: WorkflowState,
    stage: str,
    error: Exception,
) -> dict:
    return {
        "current_stage": stage,
        "errors": [*state.get("errors", []), _workflow_error(stage, error)],
    }


def _workflow_error(stage: str, error: Exception) -> WorkflowError:
    diagnostics: dict[str, str | int | float | None] = {}
    for name in ("exit_code", "stdout", "stderr", "timeout"):
        value = getattr(error, name, None)
        if value is not None:
            diagnostics[name] = value
    return WorkflowError(
        stage=stage,
        error_type=type(error).__name__,
        message=str(error),
        diagnostics=diagnostics,
    )
