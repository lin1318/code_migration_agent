"""Allowlisted Go command execution and structured diagnostic parsing."""

from __future__ import annotations

import hashlib
import re
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from models.go_validation import (
    DiagnosticCategory,
    DiagnosticClassification,
    GoCommandResult,
    GoDiagnostic,
    ValidationSummary,
)

_FILE_DIAGNOSTIC = re.compile(
    r"^(?P<file>.+?\.go):(?P<line>\d+)(?::(?P<column>\d+))?:\s*(?P<message>.+)$"
)
_TEST_FAILURE = re.compile(r"^--- FAIL: (?P<test>\S+)(?: \([^)]*\))?")
_PACKAGE_FAILURE = re.compile(r"^FAIL\s+(?P<package>\S+)")
_ALLOWED_EXECUTABLES = {"go", "gofmt", "goimports", "git"}


def run_validation_command(
    operation: str,
    command: list[str],
    repository: Path,
    timeout_seconds: float,
) -> GoCommandResult:
    if not command or command[0] not in _ALLOWED_EXECUTABLES:
        raise ValueError(f"command is not allowlisted: {command!r}")
    started_at = datetime.now(UTC)
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=repository,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
            timeout=timeout_seconds,
        )
        return GoCommandResult(
            operation=operation,
            command=command,
            working_directory=str(repository),
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_seconds=time.monotonic() - started,
            timed_out=False,
            started=started_at.isoformat(),
            completed=datetime.now(UTC).isoformat(),
        )
    except subprocess.TimeoutExpired as error:
        return GoCommandResult(
            operation=operation,
            command=command,
            working_directory=str(repository),
            exit_code=None,
            stdout=_text(error.stdout),
            stderr=_text(error.stderr),
            duration_seconds=time.monotonic() - started,
            timed_out=True,
            started=started_at.isoformat(),
            completed=datetime.now(UTC).isoformat(),
        )
    except FileNotFoundError as error:
        return GoCommandResult(
            operation=operation,
            command=command,
            working_directory=str(repository),
            exit_code=None,
            stdout="",
            stderr=str(error),
            duration_seconds=time.monotonic() - started,
            timed_out=False,
            started=started_at.isoformat(),
            completed=datetime.now(UTC).isoformat(),
        )


def skipped_command(
    operation: str,
    repository: Path,
    reason: str,
) -> GoCommandResult:
    now = datetime.now(UTC).isoformat()
    return GoCommandResult(
        operation=operation,
        command=[],
        working_directory=str(repository),
        exit_code=None,
        stdout="",
        stderr="",
        duration_seconds=0,
        timed_out=False,
        started=now,
        completed=now,
        skipped=True,
        skip_reason=reason,
    )


def parse_command_diagnostics(
    result: GoCommandResult,
    command_index: int,
    repository: Path,
) -> list[GoDiagnostic]:
    if result.skipped or (result.exit_code == 0 and not result.timed_out):
        return []
    lines = [
        line
        for line in (result.stdout + "\n" + result.stderr).splitlines()
        if line.strip()
    ]
    if result.timed_out:
        return [
            _diagnostic(
                DiagnosticCategory.COMMAND_TIMEOUT,
                result.operation,
                "command timed out",
                "command timed out",
                command_index,
            )
        ]
    if result.exit_code is None:
        raw = lines[-1] if lines else "command could not be executed"
        return [
            _diagnostic(
                DiagnosticCategory.COMMAND_EXECUTION_ERROR,
                result.operation,
                raw.strip(),
                raw,
                command_index,
            )
        ]
    diagnostics: list[GoDiagnostic] = []
    current_package: str | None = None
    for raw_line in lines:
        line = raw_line.strip()
        package_match = _PACKAGE_FAILURE.match(line)
        if package_match:
            current_package = package_match.group("package")
        test_match = _TEST_FAILURE.match(line)
        if test_match:
            diagnostics.append(
                _diagnostic(
                    DiagnosticCategory.TEST_FAILURE,
                    result.operation,
                    f"test failed: {test_match.group('test')}",
                    raw_line,
                    command_index,
                    package=current_package,
                    test_name=test_match.group("test"),
                )
            )
            continue
        file_match = _FILE_DIAGNOSTIC.match(line)
        if file_match:
            file = _relative_file(file_match.group("file"), repository)
            category = _operation_category(result.operation)
            diagnostics.append(
                _diagnostic(
                    category,
                    result.operation,
                    file_match.group("message"),
                    raw_line,
                    command_index,
                    package=current_package,
                    file=file,
                    line=int(file_match.group("line")),
                    column=(
                        int(file_match.group("column"))
                        if file_match.group("column")
                        else None
                    ),
                )
            )
    if not diagnostics:
        raw = lines[-1] if lines else "command exited unsuccessfully"
        diagnostics.append(
            _diagnostic(
                _operation_category(result.operation, unknown=True),
                result.operation,
                raw.strip(),
                raw,
                command_index,
                package=current_package,
            )
        )
    return diagnostics


def classify_diagnostics(
    post: list[GoDiagnostic],
    baseline: list[GoDiagnostic] | None,
) -> tuple[list[GoDiagnostic], list[GoDiagnostic], list[GoDiagnostic], list[GoDiagnostic]]:
    if baseline is None:
        unclassified = [
            item.model_copy(
                update={"classification": DiagnosticClassification.UNCLASSIFIED}
            )
            for item in post
        ]
        return [], [], [], unclassified
    baseline_by_key = {item.normalized_key: item for item in baseline}
    post_by_key = {item.normalized_key: item for item in post}
    introduced = [
        item.model_copy(update={"classification": DiagnosticClassification.INTRODUCED})
        for key, item in post_by_key.items()
        if key not in baseline_by_key
    ]
    persistent = [
        item.model_copy(update={"classification": DiagnosticClassification.PERSISTENT})
        for key, item in post_by_key.items()
        if key in baseline_by_key
    ]
    resolved = [
        item.model_copy(update={"classification": DiagnosticClassification.RESOLVED})
        for key, item in baseline_by_key.items()
        if key not in post_by_key
    ]
    return introduced, persistent, resolved, []


def validation_summary(
    commands: list[GoCommandResult],
    diagnostics: list[GoDiagnostic],
) -> ValidationSummary:
    return ValidationSummary(
        command_count=len(commands),
        passed_command_count=sum(item.status.value == "passed" for item in commands),
        failed_command_count=sum(item.status.value == "failed" for item in commands),
        timed_out_command_count=sum(
            item.status.value == "timed_out" for item in commands
        ),
        skipped_command_count=sum(item.skipped for item in commands),
        diagnostic_count=len(diagnostics),
    )


def _diagnostic(
    category: DiagnosticCategory,
    operation: str,
    message: str,
    raw_line: str,
    command_index: int,
    *,
    package: str | None = None,
    file: str | None = None,
    line: int | None = None,
    column: int | None = None,
    test_name: str | None = None,
) -> GoDiagnostic:
    normalized_message = " ".join(message.split())
    normalized_key = "|".join(
        [
            category.value,
            package or "",
            file or "",
            str(line or ""),
            str(column or ""),
            test_name or "",
            normalized_message,
        ]
    )
    diagnostic_id = "go-diagnostic-" + hashlib.sha256(
        normalized_key.encode("utf-8")
    ).hexdigest()[:16]
    return GoDiagnostic(
        diagnostic_id=diagnostic_id,
        category=category,
        operation=operation,
        package=package,
        file=file,
        line=line,
        column=column,
        test_name=test_name,
        message=message,
        raw_line=raw_line,
        command_index=command_index,
        normalized_key=normalized_key,
    )


def _operation_category(
    operation: str,
    *,
    unknown: bool = False,
) -> DiagnosticCategory:
    if operation in {"gofmt", "goimports"}:
        return DiagnosticCategory.FORMATTING_ERROR
    if operation == "go_mod_tidy":
        return DiagnosticCategory.DEPENDENCY_ERROR
    if operation == "go_vet_repository":
        return DiagnosticCategory.VET_ERROR
    if "test" in operation:
        return DiagnosticCategory.TEST_FAILURE
    if "package" in operation and unknown:
        return DiagnosticCategory.PACKAGE_LOADING_ERROR
    if "build" in operation:
        return DiagnosticCategory.COMPILER_ERROR
    return (
        DiagnosticCategory.UNKNOWN_ERROR
        if unknown
        else DiagnosticCategory.COMMAND_EXECUTION_ERROR
    )


def _relative_file(value: str, repository: Path) -> str:
    path = Path(value)
    if path.is_absolute():
        try:
            return path.resolve().relative_to(repository.resolve()).as_posix()
        except ValueError:
            return path.name
    return value.replace("\\", "/").removeprefix("./")


def _text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""
