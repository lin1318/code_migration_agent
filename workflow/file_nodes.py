"""Thin LangGraph nodes for the existing file migration services."""

from __future__ import annotations

from pathlib import Path

from agent.file_bundle_execution import FileBundleModelClient
from agent.file_bundle_runner import run_file_migration
from knowledge.registry import RuleRegistry, load_rule_registry
from models.file_migration_execution import (
    FileBundleRunStatus,
    FileMigrationRunStatus,
)
from models.file_migration_validation import (
    FileMigrationWorkflowReport,
    FileMigrationWorkflowStatus,
)
from models.file_migration_workflow import FileMigrationGraphState
from models.workflow import WorkflowError
from tools.file_bundle_planner import build_file_bundle_plan
from tools.file_classifier import classify_gin_files
from tools.file_migration_planner import build_file_migration_plan
from tools.file_migration_validation import validate_file_migration
from tools.inspection import inspect_repository


def validate_input(state: FileMigrationGraphState) -> dict:
    stage = "validate_input"
    updates = _reset(stage)
    repository = Path(state["repository_path"]).expanduser()
    output = Path(state["output_directory"]).expanduser()
    rules = Path(state["rule_registry_path"]).expanduser()
    error: Exception | None = None
    if not repository.exists():
        error = ValueError(f"repository path does not exist: {repository}")
    elif not repository.is_dir():
        error = ValueError(f"repository path is not a directory: {repository}")
    elif output.exists():
        error = ValueError(f"output directory already exists: {output}")
    else:
        repository = repository.resolve()
        output = output.resolve()
        if output == repository or repository in output.parents:
            error = ValueError(
                "output directory must be outside the migrated repository"
            )
        elif not rules.exists() or not (rules.is_file() or rules.is_dir()):
            error = ValueError(f"rule registry path is invalid: {rules}")
        elif any(
            state[name] <= 0
            for name in (
                "analyzer_timeout",
                "apply_timeout",
                "validation_timeout",
            )
        ):
            error = ValueError("workflow timeouts must be greater than zero")
    if error is not None:
        updates["errors"] = [_error(stage, error)]
        return updates
    updates.update(
        repository_path=str(repository),
        output_directory=str(output),
        rule_registry_path=str(rules.resolve()),
        completed_stages=[stage],
    )
    return updates


def inspect(state: FileMigrationGraphState) -> dict:
    stage = "inspect_repository"
    try:
        result = inspect_repository(
            state["repository_path"],
            analyzer_timeout=state["analyzer_timeout"],
            rule_path=state["rule_registry_path"],
        )
    except Exception as error:
        return _failure(state, stage, error)
    return _success(state, stage, inspection=result)


def load_rules(state: FileMigrationGraphState) -> dict:
    stage = "load_migration_rules"
    try:
        registry = load_rule_registry(state["rule_registry_path"])
    except Exception as error:
        return _failure(state, stage, error)
    return _success(state, stage, migration_rules=list(registry.rules))


def classify_files(state: FileMigrationGraphState) -> dict:
    stage = "classify_files"
    try:
        classification = classify_gin_files(state["inspection"])
    except Exception as error:
        return _failure(state, stage, error)
    return _success(state, stage, classification=classification)


def build_file_plan(state: FileMigrationGraphState) -> dict:
    stage = "build_file_plan"
    try:
        plan = build_file_migration_plan(state["classification"])
    except Exception as error:
        return _failure(state, stage, error)
    return _success(state, stage, file_plan=plan)


def build_bundle_plan(state: FileMigrationGraphState) -> dict:
    stage = "build_bundle_plan"
    try:
        registry = RuleRegistry(state["migration_rules"])
        plan = build_file_bundle_plan(
            state["classification"],
            state["file_plan"],
            state["inspection"],
            registry,
        )
    except Exception as error:
        return _failure(state, stage, error)
    return _success(state, stage, bundle_plan=plan)


def prepare_output(state: FileMigrationGraphState) -> dict:
    stage = "prepare_output"
    try:
        Path(state["output_directory"]).mkdir(parents=True, exist_ok=False)
    except Exception as error:
        return _failure(state, stage, error)
    return _success(state, stage)


def execute_bundles(
    state: FileMigrationGraphState,
    model_client: FileBundleModelClient,
) -> dict:
    stage = "execute_bundles"
    try:
        result = run_file_migration(
            state["repository_path"],
            state["inspection"],
            state["bundle_plan"],
            RuleRegistry(state["migration_rules"]),
            model_client,
            allow_dirty=state["allow_dirty"],
            timeout_seconds=state["apply_timeout"],
        )
    except Exception as error:
        return _failure(state, stage, error)
    return _success(state, stage, execution=result)


def validate_repository(state: FileMigrationGraphState) -> dict:
    stage = "validate_repository"
    try:
        changed = [
            record.file
            for record in state["execution"].records
            if record.status is FileBundleRunStatus.APPLIED
        ]
        result = validate_file_migration(
            Path(state["repository_path"]),
            changed,
            timeout_seconds=state["validation_timeout"],
        )
    except Exception as error:
        return _failure(state, stage, error)
    return _success(state, stage, validation=result)


def generate_report(state: FileMigrationGraphState) -> dict:
    stage = "generate_report"
    execution = state["execution"]
    validation = state.get("validation")
    if execution.status is FileMigrationRunStatus.STOPPED:
        status = FileMigrationWorkflowStatus.MIGRATION_STOPPED
    elif validation is None:
        return _failure(state, stage, ValueError("validation result is missing"))
    else:
        status = validation.status
    report = FileMigrationWorkflowReport(
        repository_path=state["repository_path"],
        status=status,
        completed_stages=[*state.get("completed_stages", []), stage],
        migration=execution,
        validation=validation,
    )
    return _success(
        state,
        stage,
        final_report=report,
        final_status=status.value,
    )


def handle_failure(state: FileMigrationGraphState) -> dict:
    return {
        "current_stage": "handle_failure",
        "final_status": "failed",
    }


def _reset(stage: str) -> dict:
    return {
        "current_stage": stage,
        "completed_stages": [],
        "errors": [],
        "inspection": None,
        "migration_rules": [],
        "classification": None,
        "file_plan": None,
        "bundle_plan": None,
        "execution": None,
        "validation": None,
        "final_report": None,
        "final_status": None,
    }


def _success(state: FileMigrationGraphState, stage: str, **values) -> dict:
    return {
        "current_stage": stage,
        "completed_stages": [*state.get("completed_stages", []), stage],
        **values,
    }


def _failure(
    state: FileMigrationGraphState,
    stage: str,
    error: Exception,
) -> dict:
    return {
        "current_stage": stage,
        "errors": [*state.get("errors", []), _error(stage, error)],
    }


def _error(stage: str, error: Exception) -> WorkflowError:
    diagnostics = {}
    details = getattr(error, "details", None)
    if details is not None:
        diagnostics = details.diagnostics
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
