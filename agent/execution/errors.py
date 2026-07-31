"""Structured failures for single-task execution."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class ExecutionErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    operation: str
    error_type: str
    message: str
    diagnostics: dict[str, str | int | float | bool | None]


class TaskExecutionError(RuntimeError):
    def __init__(
        self,
        operation: str,
        message: str,
        *,
        diagnostics: dict[str, str | int | float | bool | None] | None = None,
    ) -> None:
        super().__init__(message)
        self.details = ExecutionErrorDetail(
            operation=operation,
            error_type=type(self).__name__,
            message=message,
            diagnostics=diagnostics or {},
        )


class TaskSelectionError(TaskExecutionError):
    pass


class ContextBuildError(TaskExecutionError):
    pass


class PromptBuildError(TaskExecutionError):
    pass


class ModelInvocationError(TaskExecutionError):
    pass


class ProposalValidationError(TaskExecutionError):
    pass


class SuspiciousNoChangeProposalError(ProposalValidationError):
    """A no-change result conflicts with the supplied source and rules."""
