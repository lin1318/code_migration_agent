from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, patch

from langgraph.checkpoint.memory import InMemorySaver

from knowledge.registry import load_rule_registry
from models.analyzer import AnalyzerResult
from models.file_migration_execution import (
    FileMigrationRunResult,
    FileMigrationRunStatus,
)
from models.file_migration_validation import (
    FileMigrationValidationResult,
    FileMigrationWorkflowStatus,
)
from models.inspection import RepositoryInspection
from tools.inspection import DEFAULT_RULE_PATH
from tools.migration_planner import build_migration_inventory
from workflow.file_graph import create_file_migration_workflow

EXPECTED_STAGES = [
    "validate_input",
    "inspect_repository",
    "load_migration_rules",
    "classify_files",
    "build_file_plan",
    "build_bundle_plan",
    "prepare_output",
    "execute_bundles",
    "validate_repository",
    "generate_report",
]


def _inspection(repository: Path) -> RepositoryInspection:
    analysis = AnalyzerResult(
        module_path="example.com/app",
        packages=[],
        usages=[],
        loading_errors=[],
    )
    inventory = build_migration_inventory(
        analysis, load_rule_registry(DEFAULT_RULE_PATH)
    )
    return RepositoryInspection.from_inventory(str(repository), [], inventory)


def _input(repository: Path, output: Path) -> dict:
    return {
        "repository_path": str(repository),
        "output_directory": str(output),
        "rule_registry_path": str(DEFAULT_RULE_PATH),
        "analyzer_timeout": 30,
        "apply_timeout": 30,
        "validation_timeout": 30,
        "allow_dirty": False,
    }


def test_successful_graph_uses_existing_services_in_stage_order(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    output = tmp_path / "output"
    inspection = _inspection(repository)
    execution = FileMigrationRunResult(
        repository_path=str(repository.resolve()),
        status=FileMigrationRunStatus.COMPLETED,
        bundle_count=0,
        completed_files=[],
        records=[],
    )
    validation = FileMigrationValidationResult(
        status=FileMigrationWorkflowStatus.COMPLETED,
        formatted_files=[],
        dependency_files_changed=[],
        command_results=[],
    )
    with (
        patch(
            "workflow.file_nodes.inspect_repository",
            return_value=inspection,
        ) as inspect_service,
        patch(
            "workflow.file_nodes.run_file_migration",
            return_value=execution,
        ) as migration_service,
        patch(
            "workflow.file_nodes.validate_file_migration",
            return_value=validation,
        ) as validation_service,
    ):
        graph = create_file_migration_workflow(Mock(model_name="fake"))
        state = graph.invoke(_input(repository, output))

    assert state["completed_stages"] == EXPECTED_STAGES
    assert state["final_status"] == "completed"
    inspect_service.assert_called_once()
    migration_service.assert_called_once()
    validation_service.assert_called_once()


def test_stopped_migration_routes_to_report_without_validation(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    stopped = FileMigrationRunResult(
        repository_path=str(repository.resolve()),
        status=FileMigrationRunStatus.STOPPED,
        bundle_count=1,
        completed_files=[],
        records=[],
        failed_bundle_id="bundle-1",
    )
    with (
        patch(
            "workflow.file_nodes.inspect_repository",
            return_value=_inspection(repository),
        ),
        patch("workflow.file_nodes.run_file_migration", return_value=stopped),
        patch("workflow.file_nodes.validate_file_migration") as validate,
    ):
        state = create_file_migration_workflow(Mock()).invoke(
            _input(repository, tmp_path / "output")
        )

    assert state["final_status"] == "migration_stopped"
    assert "validate_repository" not in state["completed_stages"]
    validate.assert_not_called()


def test_failure_routes_to_handler_and_checkpoint_threads_are_isolated(
    tmp_path: Path,
) -> None:
    saver = InMemorySaver()
    graph = create_file_migration_workflow(Mock(), checkpointer=saver)
    first_config = {"configurable": {"thread_id": "first"}}
    second_config = {"configurable": {"thread_id": "second"}}

    first = graph.invoke(
        _input(tmp_path / "missing", tmp_path / "out"), first_config
    )
    second = graph.invoke(
        _input(tmp_path / "also-missing", tmp_path / "out-2"), second_config
    )

    assert first["final_status"] == "failed"
    assert first["current_stage"] == "handle_failure"
    assert second["repository_path"] != first["repository_path"]
    assert len(list(graph.get_state_history(first_config))) >= 2
    assert len(list(graph.get_state_history(second_config))) >= 2
