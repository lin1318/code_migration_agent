"""Subprocess adapter for the semantic Go repository analyzer."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from os import PathLike
from pathlib import Path
from typing import Sequence

from pydantic import ValidationError

from models.analyzer import AnalyzerResult, PackageLoadingError

DEFAULT_TIMEOUT_SECONDS = 30.0
AnalyzerCommand = str | PathLike[str] | Sequence[str | PathLike[str]]


@dataclass(frozen=True)
class AnalyzerExecution:
    """A successful analyzer execution and its captured process output."""

    result: AnalyzerResult
    stdout: str
    stderr: str
    exit_code: int


class AnalyzerError(RuntimeError):
    """Base class for errors raised by the Go analyzer adapter."""


class AnalyzerNotFoundError(AnalyzerError):
    pass


class AnalyzerTimeoutError(AnalyzerError):
    def __init__(self, timeout: float, stdout: str, stderr: str) -> None:
        super().__init__(f"Go analyzer timed out after {timeout:g} seconds")
        self.timeout = timeout
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code: int | None = None


class AnalyzerInvalidJSONError(AnalyzerError):
    def __init__(self, message: str, stdout: str, stderr: str, exit_code: int) -> None:
        super().__init__(f"Go analyzer returned invalid JSON: {message}")
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code


class GoPackageLoadingError(AnalyzerError):
    def __init__(
        self,
        errors: list[PackageLoadingError],
        stdout: str,
        stderr: str,
        exit_code: int,
    ) -> None:
        details = "; ".join(
            f"{error.package + ': ' if error.package else ''}{error.message}"
            for error in errors
        )
        super().__init__(f"Go package loading failed: {details}")
        self.errors = errors
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code


class AnalyzerProcessError(AnalyzerError):
    def __init__(self, stdout: str, stderr: str, exit_code: int) -> None:
        message = stderr.strip() or "no diagnostic output"
        super().__init__(f"Go analyzer exited with code {exit_code}: {message}")
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code


def run_go_analyzer(
    repository_path: str | PathLike[str],
    *,
    analyzer_command: AnalyzerCommand = "analyzer",
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
    working_directory: str | PathLike[str] | None = None,
) -> AnalyzerExecution:
    """Run and validate the Go analyzer for a repository."""

    if timeout <= 0:
        raise ValueError("timeout must be greater than zero")

    command = _normalize_command(analyzer_command)
    command.append(str(repository_path))
    try:
        completed = subprocess.run(
            command,
            cwd=working_directory,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            check=False,
            shell=False,
        )
    except FileNotFoundError as error:
        raise AnalyzerNotFoundError(
            f"Go analyzer executable not found: {command[0]}"
        ) from error
    except subprocess.TimeoutExpired as error:
        raise AnalyzerTimeoutError(
            timeout,
            _output_text(error.stdout),
            _output_text(error.stderr),
        ) from error

    try:
        result = AnalyzerResult.model_validate_json(completed.stdout)
    except (ValidationError, json.JSONDecodeError, ValueError) as error:
        raise AnalyzerInvalidJSONError(
            str(error),
            completed.stdout,
            completed.stderr,
            completed.returncode,
        ) from error

    if result.loading_errors:
        raise GoPackageLoadingError(
            result.loading_errors,
            completed.stdout,
            completed.stderr,
            completed.returncode,
        )
    if completed.returncode != 0:
        raise AnalyzerProcessError(
            completed.stdout,
            completed.stderr,
            completed.returncode,
        )

    return AnalyzerExecution(
        result=result,
        stdout=completed.stdout,
        stderr=completed.stderr,
        exit_code=completed.returncode,
    )


def _normalize_command(command: AnalyzerCommand) -> list[str]:
    if isinstance(command, (str, PathLike)):
        return [str(command)]
    normalized = [str(part) for part in command]
    if not normalized:
        raise ValueError("analyzer_command must not be empty")
    return normalized


def _output_text(output: str | bytes | None) -> str:
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode("utf-8", errors="replace")
    return output
