from pathlib import Path
from unittest.mock import patch

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from knowledge.registry import load_rule_registry
from models.analyzer import AnalyzerResult, PackageLoadingError
from tools.go_analyzer import (
    AnalyzerExecution,
    AnalyzerInvalidJSONError,
    AnalyzerNotFoundError,
    AnalyzerProcessError,
    AnalyzerTimeoutError,
    GoPackageLoadingError,
)
from tools.migration_planner import build_migration_inventory
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
