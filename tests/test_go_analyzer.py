import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from tools.go_analyzer import (
    AnalyzerInvalidJSONError,
    AnalyzerNotFoundError,
    AnalyzerTimeoutError,
    GoPackageLoadingError,
    run_go_analyzer,
)


def analyzer_json(**overrides: object) -> str:
    payload: dict[str, object] = {
        "module_path": "example.com/app",
        "packages": [
            {
                "import_path": "example.com/app",
                "name": "app",
                "source_files": ["/repo/app.go"],
                "imports": [],
            }
        ],
        "usages": [],
        "loading_errors": [],
    }
    payload.update(overrides)
    return json.dumps(payload)


@patch("tools.go_analyzer.subprocess.run")
def test_run_validates_output_and_captures_process(mock_run) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=["analyzer", "/repo"], returncode=0, stdout=analyzer_json(), stderr="note"
    )

    execution = run_go_analyzer("/repo", timeout=4)

    assert execution.result.module_path == "example.com/app"
    assert execution.stdout
    assert execution.stderr == "note"
    assert execution.exit_code == 0
    mock_run.assert_called_once_with(
        ["analyzer", "/repo"],
        cwd=None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=4,
        check=False,
        shell=False,
    )


@patch("tools.go_analyzer.subprocess.run", side_effect=FileNotFoundError)
def test_analyzer_not_found_has_clear_error(_mock_run) -> None:
    with pytest.raises(AnalyzerNotFoundError, match="not found: missing-analyzer"):
        run_go_analyzer(".", analyzer_command="missing-analyzer")


@patch("tools.go_analyzer.subprocess.run")
def test_timeout_preserves_captured_output(mock_run) -> None:
    mock_run.side_effect = subprocess.TimeoutExpired(
        cmd=["analyzer"], timeout=2, output=b"partial", stderr=b"slow"
    )

    with pytest.raises(AnalyzerTimeoutError, match="timed out after 2 seconds") as caught:
        run_go_analyzer(".", timeout=2)

    assert caught.value.stdout == "partial"
    assert caught.value.stderr == "slow"
    assert caught.value.exit_code is None


@patch("tools.go_analyzer.subprocess.run")
def test_invalid_json_has_process_diagnostics(mock_run) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=["analyzer"], returncode=1, stdout="{bad", stderr="load failed"
    )

    with pytest.raises(AnalyzerInvalidJSONError, match="invalid JSON") as caught:
        run_go_analyzer(".")

    assert caught.value.exit_code == 1
    assert caught.value.stderr == "load failed"


@patch("tools.go_analyzer.subprocess.run")
def test_schema_validation_rejects_unexpected_fields(mock_run) -> None:
    mock_run.return_value = subprocess.CompletedProcess(
        args=["analyzer"],
        returncode=0,
        stdout=analyzer_json(unexpected=True),
        stderr="",
    )

    with pytest.raises(AnalyzerInvalidJSONError, match="invalid JSON"):
        run_go_analyzer(".")


@patch("tools.go_analyzer.subprocess.run")
def test_package_loading_errors_are_structured(mock_run) -> None:
    loading_errors = [
        {
            "kind": "type",
            "package": "example.com/app",
            "position": "app.go:4:2",
            "message": "undefined: missing",
        }
    ]
    mock_run.return_value = subprocess.CompletedProcess(
        args=["analyzer"],
        returncode=1,
        stdout=analyzer_json(loading_errors=loading_errors),
        stderr="",
    )

    with pytest.raises(GoPackageLoadingError, match="undefined: missing") as caught:
        run_go_analyzer(".")

    assert caught.value.errors[0].kind == "type"
    assert caught.value.exit_code == 1


def test_real_go_analyzer_execution() -> None:
    project_root = Path(__file__).resolve().parents[1]
    go_analyzer = project_root / "go-analyzer"
    fixture = go_analyzer / "analyzer" / "testdata" / "gin-positive"

    execution = run_go_analyzer(
        fixture,
        analyzer_command=("go", "run", "./cmd/analyzer"),
        timeout=60,
        working_directory=go_analyzer,
    )

    assert execution.exit_code == 0
    assert execution.result.module_path == "example.com/gin-positive"
    assert any(
        usage.symbol == "github.com/gin-gonic/gin.(*Context).JSON"
        for usage in execution.result.usages
    )
