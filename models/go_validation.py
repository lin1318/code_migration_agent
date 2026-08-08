"""Typed result for allowlisted Go validation commands."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class ValidationModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CommandStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    SKIPPED = "skipped"


class GoCommandResult(ValidationModel):
    operation: str
    command: list[str]
    working_directory: str
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float = Field(ge=0)
    timed_out: bool
    started: str
    completed: str
    skipped: bool = False
    skip_reason: str | None = None

    @property
    def status(self) -> CommandStatus:
        if self.skipped:
            return CommandStatus.SKIPPED
        if self.timed_out:
            return CommandStatus.TIMED_OUT
        return CommandStatus.PASSED if self.exit_code == 0 else CommandStatus.FAILED
