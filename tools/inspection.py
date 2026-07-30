"""Repository-facing orchestration for deterministic inspection and planning."""

from __future__ import annotations

from os import PathLike
from pathlib import Path

from knowledge.registry import load_rule_registry
from models.analyzer import AnalyzerResult
from models.inspection import RepositoryInspection
from models.migration_plan import MigrationInventory, MigrationPhase
from tools.go_analyzer import AnalyzerCommand, run_go_analyzer
from tools.migration_planner import build_migration_inventory

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RULE_PATH = PROJECT_ROOT / "knowledge"
DEFAULT_ANALYZER_COMMAND = ("go", "run", "./cmd/analyzer")
DEFAULT_ANALYZER_DIRECTORY = PROJECT_ROOT / "go-analyzer"


class InspectionError(RuntimeError):
    pass


class InvalidRepositoryError(InspectionError):
    pass


class PlanningInvariantError(InspectionError):
    pass


def inspect_repository(
    repository_path: str | PathLike[str],
    *,
    analyzer_timeout: float,
    analyzer_command: AnalyzerCommand = DEFAULT_ANALYZER_COMMAND,
    analyzer_directory: str | PathLike[str] = DEFAULT_ANALYZER_DIRECTORY,
    rule_path: str | PathLike[str] = DEFAULT_RULE_PATH,
) -> RepositoryInspection:
    """Analyze a repository and build its deterministic migration plan."""

    repository = Path(repository_path).expanduser()
    if not repository.exists():
        raise InvalidRepositoryError(f"repository path does not exist: {repository}")
    if not repository.is_dir():
        raise InvalidRepositoryError(
            f"repository path is not a directory: {repository}"
        )
    repository = repository.resolve()

    execution = run_go_analyzer(
        repository,
        analyzer_command=analyzer_command,
        timeout=analyzer_timeout,
        working_directory=analyzer_directory,
    )
    registry = load_rule_registry(rule_path)
    inventory = build_migration_inventory(execution.result, registry)
    _validate_plan(inventory)
    packages = _packages_containing_gin_usages(execution.result)
    return RepositoryInspection.from_inventory(
        str(repository),
        packages,
        inventory,
    )


def _validate_plan(inventory: MigrationInventory) -> None:
    expected = list(MigrationPhase)
    actual = [task.phase for task in inventory.tasks]
    if actual != expected or [task.order for task in inventory.tasks] != list(
        range(1, 11)
    ):
        raise PlanningInvariantError(
            "migration task ordering does not match the required ten phases"
        )
    classified = (
        len(inventory.exact_migrations)
        + len(inventory.migrations_requiring_review)
        + len(inventory.unsupported_usages)
        + len(inventory.unmatched_usages)
    )
    if classified != len(inventory.discovered_gin_usages):
        raise PlanningInvariantError(
            "not every discovered Gin usage was classified exactly once"
        )


def _packages_containing_gin_usages(analysis: AnalyzerResult) -> list[str]:
    usage_files = {_normalized_path(usage.file) for usage in analysis.usages}
    packages: set[str] = set()
    for package in analysis.packages:
        for source_file in package.source_files:
            normalized = _normalized_path(source_file)
            if any(
                normalized == usage_file
                or normalized.endswith("/" + usage_file)
                for usage_file in usage_files
            ):
                packages.add(package.import_path)
                break
    return sorted(packages)


def _normalized_path(path: str) -> str:
    return path.replace("\\", "/").casefold()
