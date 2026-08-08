"""Allowlisted command execution used by final file-migration validation."""

from __future__ import annotations

import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

from models.go_validation import GoCommandResult

_ALLOWED_EXECUTABLES = {"go", "gofmt"}


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
        return _result(
            operation,
            command,
            repository,
            started_at,
            started,
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
    except subprocess.TimeoutExpired as error:
        return _result(
            operation,
            command,
            repository,
            started_at,
            started,
            exit_code=None,
            stdout=_text(error.stdout),
            stderr=_text(error.stderr),
            timed_out=True,
        )
    except FileNotFoundError as error:
        return _result(
            operation,
            command,
            repository,
            started_at,
            started,
            exit_code=None,
            stdout="",
            stderr=str(error),
        )


def _result(
    operation: str,
    command: list[str],
    repository: Path,
    started_at: datetime,
    started: float,
    *,
    exit_code: int | None,
    stdout: str,
    stderr: str,
    timed_out: bool = False,
) -> GoCommandResult:
    return GoCommandResult(
        operation=operation,
        command=command,
        working_directory=str(repository),
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=time.monotonic() - started,
        timed_out=timed_out,
        started=started_at.isoformat(),
        completed=datetime.now(UTC).isoformat(),
    )


def _text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode(errors="replace") if isinstance(value, bytes) else value
