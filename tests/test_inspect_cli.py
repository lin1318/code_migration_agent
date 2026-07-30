import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.cli import main
from knowledge.registry import RuleFileError
from models.analyzer import AnalyzerResult
from tools.go_analyzer import (
    AnalyzerExecution,
    AnalyzerInvalidJSONError,
    AnalyzerProcessError,
    AnalyzerTimeoutError,
)
from tools.inspection import PlanningInvariantError


def analysis_for(repository: Path, *, with_gin: bool = True) -> AnalyzerResult:
    usages = (
        [
            {
                "file": "main.go",
                "line": 4,
                "column": 7,
                "kind": "constructor",
                "symbol": "github.com/gin-gonic/gin.Default",
            }
        ]
        if with_gin
        else []
    )
    return AnalyzerResult.model_validate(
        {
            "module_path": "example.com/app",
            "packages": [
                {
                    "import_path": "example.com/app",
                    "name": "main",
                    "source_files": [str(repository / "main.go")],
                    "imports": ["github.com/gin-gonic/gin"] if with_gin else [],
                }
            ],
            "usages": usages,
            "loading_errors": [],
        }
    )


def execution_for(repository: Path, *, with_gin: bool = True) -> AnalyzerExecution:
    result = analysis_for(repository, with_gin=with_gin)
    return AnalyzerExecution(
        result=result,
        stdout=result.model_dump_json(),
        stderr="",
        exit_code=0,
    )


@patch("tools.inspection.run_go_analyzer")
def test_successful_repository_inspection(mock_analyzer, tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    mock_analyzer.return_value = execution_for(repository)
    stdout, stderr = io.StringIO(), io.StringIO()

    exit_code = main(
        ["inspect", "--repo", str(repository), "--analyzer-timeout", "9"],
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert stderr.getvalue() == ""
    assert f"Repository path: {repository.resolve()}" in stdout.getvalue()
    assert "Module path: example.com/app" in stdout.getvalue()
    assert "Packages containing Gin usages: example.com/app" in stdout.getvalue()
    assert "Total discovered Gin usages: 1" in stdout.getvalue()
    assert "Ordered migration task count: 10" in stdout.getvalue()
    assert mock_analyzer.call_args.kwargs["timeout"] == 9


@patch("tools.inspection.run_go_analyzer")
def test_repository_with_no_gin_usages(mock_analyzer, tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    mock_analyzer.return_value = execution_for(repository, with_gin=False)
    stdout = io.StringIO()

    exit_code = main(
        ["inspect", "--repo", str(repository)],
        stdout=stdout,
        stderr=io.StringIO(),
    )

    assert exit_code == 0
    assert "Packages containing Gin usages: none" in stdout.getvalue()
    assert "Total discovered Gin usages: 0" in stdout.getvalue()
    assert "Exact migration count: 0" in stdout.getvalue()


@patch("tools.inspection.run_go_analyzer")
def test_invalid_repository_path_does_not_run_analyzer(
    mock_analyzer, tmp_path: Path
) -> None:
    stderr = io.StringIO()

    exit_code = main(
        ["inspect", "--repo", str(tmp_path / "missing")],
        stdout=io.StringIO(),
        stderr=stderr,
    )

    assert exit_code != 0
    assert "does not exist" in stderr.getvalue()
    mock_analyzer.assert_not_called()


@patch("tools.inspection.run_go_analyzer")
def test_repository_path_must_be_directory(mock_analyzer, tmp_path: Path) -> None:
    repository_file = tmp_path / "main.go"
    repository_file.write_text("package main\n", encoding="utf-8")
    stderr = io.StringIO()

    exit_code = main(
        ["inspect", "--repo", str(repository_file)],
        stdout=io.StringIO(),
        stderr=stderr,
    )

    assert exit_code != 0
    assert "not a directory" in stderr.getvalue()
    mock_analyzer.assert_not_called()


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (AnalyzerProcessError("", "analyzer failed", 7), "exited with code 7"),
        (
            AnalyzerTimeoutError(3, "partial", "slow"),
            "timed out after 3 seconds",
        ),
        (
            AnalyzerInvalidJSONError("malformed", "{", "bad output", 1),
            "invalid JSON",
        ),
    ],
)
@patch("tools.inspection.run_go_analyzer")
def test_analyzer_failures_return_nonzero(
    mock_analyzer,
    error: Exception,
    message: str,
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    mock_analyzer.side_effect = error
    stderr = io.StringIO()

    exit_code = main(
        ["inspect", "--repo", str(repository)],
        stdout=io.StringIO(),
        stderr=stderr,
    )

    assert exit_code != 0
    assert message in stderr.getvalue()


@patch("tools.inspection.build_migration_inventory")
@patch("tools.inspection.run_go_analyzer")
def test_planner_failure_returns_nonzero(
    mock_analyzer,
    mock_planner,
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    mock_analyzer.return_value = execution_for(repository)
    mock_planner.side_effect = PlanningInvariantError("bad task ordering")
    stderr = io.StringIO()

    exit_code = main(
        ["inspect", "--repo", str(repository)],
        stdout=io.StringIO(),
        stderr=stderr,
    )

    assert exit_code != 0
    assert "bad task ordering" in stderr.getvalue()


@patch("tools.inspection.load_rule_registry")
@patch("tools.inspection.run_go_analyzer")
def test_invalid_rule_registry_returns_nonzero(
    mock_analyzer,
    mock_registry,
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    mock_analyzer.return_value = execution_for(repository)
    mock_registry.side_effect = RuleFileError("invalid rule file rules.yaml")
    stderr = io.StringIO()

    exit_code = main(
        ["inspect", "--repo", str(repository)],
        stdout=io.StringIO(),
        stderr=stderr,
    )

    assert exit_code != 0
    assert "invalid rule file" in stderr.getvalue()


@patch("tools.inspection.run_go_analyzer")
def test_generates_complete_json_output(mock_analyzer, tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    mock_analyzer.return_value = execution_for(repository)
    output = tmp_path / "migration-plan.json"

    exit_code = main(
        ["inspect", "--repo", str(repository), "--output", str(output)],
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert exit_code == 0
    document = json.loads(output.read_text(encoding="utf-8"))
    assert set(document) == {
        "repository_path",
        "repository_summary",
        "packages_containing_gin_usages",
        "discovered_gin_usages",
        "exact_migrations",
        "migrations_requiring_review",
        "unsupported_usages",
        "unmatched_usages",
        "ordered_migration_tasks",
    }
    assert len(document["ordered_migration_tasks"]) == 10


@patch("tools.inspection.run_go_analyzer")
def test_refuses_output_inside_inspected_repository(
    mock_analyzer, tmp_path: Path
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    mock_analyzer.return_value = execution_for(repository)

    exit_code = main(
        [
            "inspect",
            "--repo",
            str(repository),
            "--output",
            str(repository / "plan.json"),
        ],
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    )

    assert exit_code != 0
    assert not (repository / "plan.json").exists()
