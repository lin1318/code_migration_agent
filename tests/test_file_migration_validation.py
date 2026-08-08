from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from models.file_migration_validation import FileMigrationWorkflowStatus
from models.go_validation import GoCommandResult
from tools.file_migration_validation import validate_file_migration


def _result(operation: str, command: list[str], repository: Path, exit_code=0):
    return GoCommandResult(
        operation=operation,
        command=command,
        working_directory=str(repository),
        exit_code=exit_code,
        stdout="out",
        stderr="err" if exit_code else "",
        duration_seconds=0.1,
        timed_out=False,
        started="2026-01-01T00:00:00+00:00",
        completed="2026-01-01T00:00:00+00:00",
    )


def test_runs_required_commands_in_order_and_only_formats_changed_go_files(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "go.mod").write_text("module example.com/app\n")
    calls = []

    def run(operation, command, repo, timeout):
        calls.append((operation, command, timeout))
        if operation == "go_mod_tidy":
            (repo / "go.sum").write_text("generated\n")
        return _result(operation, command, repo)

    with patch(
        "tools.file_migration_validation.run_validation_command",
        side_effect=run,
    ):
        result = validate_file_migration(
            repository,
            ["b.go", "notes.txt", "a.go", "b.go"],
            timeout_seconds=42,
        )

    assert [item[0] for item in calls] == [
        "gofmt",
        "go_mod_tidy",
        "go_test",
        "go_vet",
    ]
    assert calls[0][1] == ["gofmt", "-w", "--", "a.go", "b.go"]
    assert all(item[2] == 42 for item in calls)
    assert result.formatted_files == ["a.go", "b.go"]
    assert result.dependency_files_changed == ["go.sum"]
    assert result.status is FileMigrationWorkflowStatus.COMPLETED


def test_nonzero_command_makes_validation_fail(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()

    def run(operation, command, repo, _timeout):
        return _result(
            operation,
            command,
            repo,
            exit_code=1 if operation == "go_test" else 0,
        )

    with patch(
        "tools.file_migration_validation.run_validation_command",
        side_effect=run,
    ):
        result = validate_file_migration(
            repository, [], timeout_seconds=10
        )

    assert [item.operation for item in result.command_results] == [
        "go_mod_tidy",
        "go_test",
        "go_vet",
    ]
    assert result.status is FileMigrationWorkflowStatus.VALIDATION_FAILED
