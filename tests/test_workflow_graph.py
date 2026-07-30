from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from knowledge.registry import load_rule_registry
from models.analyzer import AnalyzerResult, PackageLoadingError
from models.workflow import WorkflowError
from tools.go_analyzer import (
    AnalyzerExecution,
    AnalyzerInvalidJSONError,
    AnalyzerNotFoundError,
    AnalyzerProcessError,
    AnalyzerTimeoutError,
    GoPackageLoadingError,
)
from tools.migration_planner import build_migration_inventory
from workflow import nodes
from workflow.graph import create_inspection_workflow


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RULES = PROJECT_ROOT / "knowledge"


def fixture_analysis() -> AnalyzerResult:
    path = PROJECT_ROOT / "tests" / "fixtures" / "analyzer_output.json"
    return AnalyzerResult.model_validate_json(path.read_text(encoding="utf-8"))


def execution(analysis: AnalyzerResult | None = None) -> AnalyzerExecution:
    result = analysis or fixture_analysis()
    return AnalyzerExecution(
        result=result,
        stdout=result.model_dump_json(),
        stderr="",
        exit_code=0,
    )


def invoke(repository: Path, rules: Path = RULES, *, checkpointer=None):
    graph = create_inspection_workflow(checkpointer)
    config = (
        {"configurable": {"thread_id": "workflow-test"}}
        if checkpointer is not None
        else None
    )
    return graph.invoke(
        {
            "repository_path": str(repository),
            "rule_registry_path": str(rules),
            "analyzer_timeout": 5.0,
        },
        config=config,
    )


@patch("workflow.nodes.run_go_analyzer")
def test_successful_workflow_execution(mock_analyzer, tmp_path: Path) -> None:
    mock_analyzer.return_value = execution()

    state = invoke(tmp_path)

    assert state["final_status"] == "success"
    assert state["current_stage"] == "generate_report"
    assert state["completed_stages"] == [
        "validate_input",
        "inspect_repository",
        "load_migration_rules",
        "build_inventory",
        "validate_inventory",
        "build_plan",
        "validate_plan",
        "generate_report",
    ]
    assert len(state["migration_plan"]) == 10
    assert state["final_report"].final_status == "success"


@patch("workflow.nodes.run_go_analyzer")
def test_repository_without_gin_usages(mock_analyzer, tmp_path: Path) -> None:
    mock_analyzer.return_value = execution(
        AnalyzerResult(
            module_path="example.com/empty",
            packages=[],
            usages=[],
            loading_errors=[],
        )
    )

    state = invoke(tmp_path)

    assert state["final_status"] == "success"
    assert state["repository_summary"].gin_usage_count == 0
    assert state["migration_inventory"].exact_migrations == []
    assert len(state["migration_plan"]) == 10


@pytest.mark.parametrize(
    ("repository_factory", "rules_factory", "message"),
    [
        (lambda root: root / "missing", lambda _root: RULES, "does not exist"),
        (
            lambda root: root,
            lambda root: root / "missing-rules",
            "rule registry path does not exist",
        ),
    ],
)
def test_invalid_input_routes_to_failure(
    tmp_path: Path,
    repository_factory,
    rules_factory,
    message: str,
) -> None:
    state = invoke(repository_factory(tmp_path), rules_factory(tmp_path))

    assert state["final_status"] == "failed"
    assert state["current_stage"] == "handle_failure"
    assert state["completed_stages"] == []
    assert state["errors"][0].stage == "validate_input"
    assert message in state["errors"][0].message


@pytest.mark.parametrize(
    "error",
    [
        AnalyzerNotFoundError("Go analyzer executable not found: analyzer"),
        AnalyzerTimeoutError(5, "partial", "slow"),
        AnalyzerProcessError("", "process failed", 7),
        AnalyzerInvalidJSONError("malformed", "{", "bad JSON", 1),
        GoPackageLoadingError(
            [
                PackageLoadingError(
                    kind="type",
                    package="example.com/app",
                    position="main.go:2:1",
                    message="undefined: missing",
                )
            ],
            "{}",
            "",
            1,
        ),
    ],
)
@patch("workflow.nodes.load_rule_registry")
@patch("workflow.nodes.run_go_analyzer")
def test_analyzer_failures_terminate_before_later_nodes(
    mock_analyzer,
    mock_load_rules,
    error: Exception,
    tmp_path: Path,
) -> None:
    mock_analyzer.side_effect = error

    state = invoke(tmp_path)

    assert state["final_status"] == "failed"
    assert state["current_stage"] == "handle_failure"
    assert state["completed_stages"] == ["validate_input"]
    assert state["errors"][0].stage == "inspect_repository"
    assert state["errors"][0].error_type == type(error).__name__
    mock_load_rules.assert_not_called()


@patch("workflow.nodes.run_go_analyzer")
def test_invalid_rule_registry_routes_to_failure(
    mock_analyzer, tmp_path: Path
) -> None:
    mock_analyzer.return_value = execution()
    invalid_rules = tmp_path / "invalid.yaml"
    invalid_rules.write_text("rules: [", encoding="utf-8")

    state = invoke(tmp_path, invalid_rules)

    assert state["final_status"] == "failed"
    assert state["errors"][0].stage == "load_migration_rules"
    assert state["errors"][0].error_type == "RuleFileError"
    assert state["completed_stages"] == [
        "validate_input",
        "inspect_repository",
    ]


@patch("workflow.nodes.run_go_analyzer")
def test_inventory_invariant_failure(mock_analyzer, tmp_path: Path) -> None:
    analysis = fixture_analysis()
    mock_analyzer.return_value = execution(analysis)
    inventory = build_migration_inventory(analysis, load_rule_registry(RULES))
    broken_summary = inventory.repository_summary.model_copy(
        update={"gin_api_usage_count": 999}
    )
    broken = inventory.model_copy(update={"repository_summary": broken_summary})

    with patch("workflow.nodes.build_migration_inventory", return_value=broken):
        state = invoke(tmp_path)

    assert state["final_status"] == "failed"
    assert state["errors"][0].stage == "validate_inventory"
    assert "do not equal total usages" in state["errors"][0].message
    assert "build_plan" not in state["completed_stages"]


@patch("workflow.nodes.run_go_analyzer")
def test_plan_invariant_failure(mock_analyzer, tmp_path: Path) -> None:
    mock_analyzer.return_value = execution()

    with patch(
        "workflow.nodes.get_ordered_migration_plan",
        side_effect=lambda inventory: list(reversed(inventory.tasks)),
    ):
        state = invoke(tmp_path)

    assert state["final_status"] == "failed"
    assert state["errors"][0].stage == "validate_plan"
    assert "required phase order" in state["errors"][0].message
    assert "generate_report" not in state["completed_stages"]


@patch("workflow.nodes.run_go_analyzer")
def test_checkpointed_output_is_deterministic(
    mock_analyzer, tmp_path: Path
) -> None:
    mock_analyzer.return_value = execution()
    first = invoke(tmp_path, checkpointer=InMemorySaver())
    second = invoke(tmp_path, checkpointer=InMemorySaver())

    assert (
        first["final_report"].model_dump(mode="json")
        == second["final_report"].model_dump(mode="json")
    )


@patch("workflow.nodes.run_go_analyzer")
def test_individual_stage_state_contents(mock_analyzer, tmp_path: Path) -> None:
    analysis = fixture_analysis()
    mock_analyzer.return_value = execution(analysis)
    state = {
        "repository_path": str(tmp_path),
        "rule_registry_path": str(RULES),
        "analyzer_timeout": 5.0,
    }

    state.update(nodes.validate_input(state))
    assert state["completed_stages"] == ["validate_input"]
    assert state["errors"] == []
    assert Path(state["repository_path"]).is_absolute()

    state.update(nodes.inspect_repository(state))
    assert state["analyzer_result"] == analysis
    assert state["completed_stages"][-1] == "inspect_repository"

    state.update(nodes.load_migration_rules(state))
    assert state["migration_rules"]
    assert state["rule_registry_metadata"].rule_count == len(
        state["migration_rules"]
    )
    assert state["completed_stages"][-1] == "load_migration_rules"

    state.update(nodes.build_inventory(state))
    assert state["migration_inventory"].discovered_gin_usages
    assert (
        state["repository_summary"]
        == state["migration_inventory"].repository_summary
    )
    assert state["completed_stages"][-1] == "build_inventory"

    state.update(nodes.validate_inventory(state))
    assert state["completed_stages"][-1] == "validate_inventory"

    state.update(nodes.build_plan(state))
    assert len(state["migration_plan"]) == 10
    assert state["completed_stages"][-1] == "build_plan"

    state.update(nodes.validate_plan(state))
    assert state["completed_stages"][-1] == "validate_plan"

    state.update(nodes.generate_report(state))
    assert state["completed_stages"][-1] == "generate_report"
    assert state["final_status"] == "success"
    assert state["final_report"].final_status == "success"


@pytest.mark.parametrize(
    "failed_stage",
    [
        "validate_input",
        "inspect_repository",
        "load_migration_rules",
        "build_inventory",
        "validate_inventory",
        "build_plan",
        "validate_plan",
        "generate_report",
    ],
)
def test_failure_at_any_stage_routes_to_handler_and_stops(
    failed_stage: str,
    tmp_path: Path,
) -> None:
    stage_order = [
        "validate_input",
        "inspect_repository",
        "load_migration_rules",
        "build_inventory",
        "validate_inventory",
        "build_plan",
        "validate_plan",
        "generate_report",
    ]
    called: list[str] = []

    def stage_node(stage: str):
        def run(state):
            called.append(stage)
            if stage == failed_stage:
                return {
                    "current_stage": stage,
                    "errors": [
                        WorkflowError(
                            stage=stage,
                            error_type="ControlledFailure",
                            message=f"{stage} failed",
                            diagnostics={"marker": stage},
                        )
                    ],
                }
            return {
                "current_stage": stage,
                "completed_stages": [
                    *state.get("completed_stages", []),
                    stage,
                ],
                "errors": [],
                "warnings": state.get("warnings", []),
            }

        return run

    with ExitStack() as stack:
        for stage in stage_order:
            stack.enter_context(
                patch(
                    f"workflow.graph.nodes.{stage}",
                    side_effect=stage_node(stage),
                )
            )
        graph = create_inspection_workflow()
        state = graph.invoke(
            {
                "repository_path": str(tmp_path),
                "rule_registry_path": str(RULES),
                "analyzer_timeout": 5.0,
            }
        )

    failed_index = stage_order.index(failed_stage)
    assert called == stage_order[: failed_index + 1]
    assert state["current_stage"] == "handle_failure"
    assert state["final_status"] == "failed"
    assert state.get("completed_stages", []) == stage_order[:failed_index]
    error = state["errors"][0]
    assert error.stage == failed_stage
    assert error.error_type == "ControlledFailure"
    assert error.message == f"{failed_stage} failed"
    assert error.diagnostics == {"marker": failed_stage}


@patch("workflow.nodes.run_go_analyzer")
def test_analyzer_timeout_preserves_diagnostics(
    mock_analyzer,
    tmp_path: Path,
) -> None:
    mock_analyzer.side_effect = AnalyzerTimeoutError(
        7.5,
        "partial analyzer output",
        "timeout diagnostic",
    )

    state = invoke(tmp_path)

    error = state["errors"][0]
    assert error.stage == "inspect_repository"
    assert error.error_type == "AnalyzerTimeoutError"
    assert "timed out" in error.message
    assert error.diagnostics == {
        "stdout": "partial analyzer output",
        "stderr": "timeout diagnostic",
        "timeout": 7.5,
    }


@patch("workflow.nodes.run_go_analyzer")
def test_memory_checkpointer_history_and_thread_isolation(
    mock_analyzer,
    tmp_path: Path,
) -> None:
    mock_analyzer.return_value = execution()
    saver = InMemorySaver()
    graph = create_inspection_workflow(saver)
    first_repo = tmp_path / "first"
    second_repo = tmp_path / "second"
    first_repo.mkdir()
    second_repo.mkdir()
    first_config = {"configurable": {"thread_id": "thread-one"}}
    second_config = {"configurable": {"thread_id": "thread-two"}}
    inputs = {
        "rule_registry_path": str(RULES),
        "analyzer_timeout": 5.0,
    }

    with pytest.raises(ValueError, match="thread_id"):
        graph.invoke({"repository_path": str(first_repo), **inputs})

    first = graph.invoke(
        {"repository_path": str(first_repo), **inputs},
        config=first_config,
    )
    second = graph.invoke(
        {"repository_path": str(second_repo), **inputs},
        config=second_config,
    )
    first_history = list(graph.get_state_history(first_config))
    second_history = list(graph.get_state_history(second_config))

    assert len(first_history) > 2
    assert len(second_history) > 2
    assert first_history[0].values["final_status"] == "success"
    assert second_history[0].values["final_status"] == "success"
    assert first_history[0].values["repository_path"] == str(first_repo.resolve())
    assert second_history[0].values["repository_path"] == str(second_repo.resolve())
    assert first["repository_path"] != second["repository_path"]


@patch("workflow.nodes.packages_containing_gin_usages", wraps=None)
@patch("workflow.nodes.validate_migration_plan", wraps=None)
@patch("workflow.nodes.get_ordered_migration_plan", wraps=None)
@patch("workflow.nodes.validate_inventory_classification", wraps=None)
@patch("workflow.nodes.build_migration_inventory", wraps=None)
@patch("workflow.nodes.load_rule_registry", wraps=None)
@patch("workflow.nodes.run_go_analyzer")
def test_nodes_delegate_to_existing_services(
    mock_analyzer,
    mock_load,
    mock_build,
    mock_validate_inventory,
    mock_get_plan,
    mock_validate_plan,
    mock_packages,
    tmp_path: Path,
) -> None:
    analysis = fixture_analysis()
    registry = load_rule_registry(RULES)
    inventory = build_migration_inventory(analysis, registry)
    mock_analyzer.return_value = execution(analysis)
    mock_load.return_value = registry
    mock_build.return_value = inventory
    mock_get_plan.return_value = inventory.tasks
    mock_packages.return_value = ["example.com/shop"]

    state = invoke(tmp_path)

    assert state["final_status"] == "success"
    for service in (
        mock_analyzer,
        mock_load,
        mock_build,
        mock_validate_inventory,
        mock_get_plan,
        mock_validate_plan,
        mock_packages,
    ):
        service.assert_called_once()
