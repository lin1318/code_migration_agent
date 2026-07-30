from pathlib import Path

from knowledge.registry import load_rule_registry
from models.analyzer import AnalyzerResult
from models.migration_plan import MigrationPhase
from tools.migration_planner import build_migration_inventory


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def fixture_analysis() -> AnalyzerResult:
    path = PROJECT_ROOT / "tests" / "fixtures" / "analyzer_output.json"
    return AnalyzerResult.model_validate_json(path.read_text(encoding="utf-8"))


def test_builds_classified_migration_inventory() -> None:
    inventory = build_migration_inventory(
        fixture_analysis(),
        load_rule_registry(PROJECT_ROOT / "knowledge"),
    )

    assert inventory.repository_summary.module_path == "example.com/shop"
    assert inventory.repository_summary.package_count == 2
    assert inventory.repository_summary.source_file_count == 2
    assert inventory.repository_summary.imports == [
        "github.com/gin-gonic/gin",
        "net/http",
    ]
    assert inventory.repository_summary.gin_usage_count == 9

    assert [item.rule_id for item in inventory.exact_migrations] == [
        "gin.default",
        "gin.context.param",
        "gin.context.json",
    ]
    assert [item.rule_id for item in inventory.migrations_requiring_review] == [
        "gin.handler_func",
        "gin.context",
        "gin.context.should_bind_json",
        "gin.context.next",
    ]
    assert not inventory.unsupported_usages
    assert [item.usage.symbol for item in inventory.unmatched_usages] == [
        "github.com/gin-gonic/gin",
        "github.com/gin-gonic/gin.(*Engine).GET",
    ]


def test_tasks_always_follow_required_order() -> None:
    inventory = build_migration_inventory(
        fixture_analysis(),
        load_rule_registry(PROJECT_ROOT / "knowledge"),
    )

    assert [task.order for task in inventory.tasks] == list(range(1, 11))
    assert [task.phase for task in inventory.tasks] == [
        MigrationPhase.DEPENDENCIES,
        MigrationPhase.SERVER_BOOTSTRAP,
        MigrationPhase.ROUTING,
        MigrationPhase.HANDLER_SIGNATURES,
        MigrationPhase.CONTEXT_OPERATIONS,
        MigrationPhase.REQUEST_BINDING,
        MigrationPhase.RESPONSES,
        MigrationPhase.MIDDLEWARE,
        MigrationPhase.TESTS,
        MigrationPhase.FINAL_VERIFICATION,
    ]
    assert inventory.tasks[0].required_imports == [
        "context",
        "github.com/cloudwego/hertz/pkg/app",
        "github.com/cloudwego/hertz/pkg/app/server",
    ]
    assert inventory.tasks[2].source_symbols == [
        "github.com/gin-gonic/gin.(*Engine).GET"
    ]
    assert inventory.tasks[8].description.startswith("Add or update tests")
    assert "go vet ./..." in inventory.tasks[9].description


def test_output_is_deterministic_when_input_usage_order_changes() -> None:
    analysis = fixture_analysis()
    reversed_analysis = analysis.model_copy(
        update={"usages": list(reversed(analysis.usages))}
    )
    registry = load_rule_registry(PROJECT_ROOT / "knowledge")

    first = build_migration_inventory(analysis, registry)
    second = build_migration_inventory(reversed_analysis, registry)

    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_empty_analysis_still_has_actionable_test_and_verification_phases() -> None:
    analysis = AnalyzerResult(
        module_path="example.com/empty",
        packages=[],
        usages=[],
        loading_errors=[],
    )

    inventory = build_migration_inventory(
        analysis,
        load_rule_registry(PROJECT_ROOT / "knowledge"),
    )

    assert inventory.repository_summary.gin_usage_count == 0
    assert not inventory.exact_migrations
    assert not inventory.migrations_requiring_review
    assert not inventory.unsupported_usages
    assert not inventory.unmatched_usages
    assert len(inventory.tasks) == 10
    assert all(task.usage_count == 0 for task in inventory.tasks)
