"""Deterministic repository validation after file migration."""

from __future__ import annotations

import hashlib
from pathlib import Path

from models.file_migration_validation import (
    FileMigrationValidationResult,
    FileMigrationWorkflowStatus,
)
from tools.go_validation import run_validation_command


def validate_file_migration(
    repository: Path,
    changed_files: list[str],
    *,
    timeout_seconds: float,
) -> FileMigrationValidationResult:
    """Format changed Go files, reconcile dependencies, test, and vet."""

    repository = repository.resolve()
    formatted_files = _safe_go_files(repository, changed_files)
    dependency_before = {
        name: _digest(repository / name) for name in ("go.mod", "go.sum")
    }
    commands: list[tuple[str, list[str]]] = []
    if formatted_files:
        commands.append(("gofmt", ["gofmt", "-w", "--", *formatted_files]))
    commands.extend(
        [
            ("go_mod_tidy", ["go", "mod", "tidy"]),
            ("go_test", ["go", "test", "./..."]),
            ("go_vet", ["go", "vet", "./..."]),
        ]
    )
    results = [
        run_validation_command(operation, command, repository, timeout_seconds)
        for operation, command in commands
    ]
    dependency_files_changed = [
        name
        for name in ("go.mod", "go.sum")
        if dependency_before[name] != _digest(repository / name)
    ]
    succeeded = all(
        result.exit_code == 0 and not result.timed_out for result in results
    )
    return FileMigrationValidationResult(
        status=(
            FileMigrationWorkflowStatus.COMPLETED
            if succeeded
            else FileMigrationWorkflowStatus.VALIDATION_FAILED
        ),
        formatted_files=formatted_files,
        dependency_files_changed=dependency_files_changed,
        command_results=results,
    )


def _digest(path: Path) -> str | None:
    if not path.is_file():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_go_files(repository: Path, changed_files: list[str]) -> list[str]:
    result: list[str] = []
    for value in dict.fromkeys(changed_files):
        if not value.endswith(".go"):
            continue
        target = (repository / value).resolve()
        if target == repository or repository not in target.parents:
            raise ValueError(f"changed file is outside repository: {value}")
        result.append(target.relative_to(repository).as_posix())
    return sorted(result)
