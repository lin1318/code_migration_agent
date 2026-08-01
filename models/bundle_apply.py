"""Typed Stage 10 request and patch-application results."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

from models.bundle_execution import BundlePatchProposal
from models.bundles import MigrationBundle


class BundleApplyModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BundleApplyStatus(StrEnum):
    DRY_RUN_SUCCESS = "dry_run_success"
    APPLIED = "applied"
    NO_CHANGE = "no_change"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


class PatchApplyRequest(BundleApplyModel):
    repository_path: str
    bundle: MigrationBundle
    proposal: BundlePatchProposal
    allow_dirty: bool = False
    dry_run: bool = False
    timeout_seconds: float = Field(default=30.0, gt=0)


class AppliedFileResult(BundleApplyModel):
    file: str
    applied: bool
    additions: int = Field(ge=0)
    deletions: int = Field(ge=0)


class GitCommandResult(BundleApplyModel):
    command: list[str]
    exit_code: int | None
    stdout: str
    stderr: str
    duration_seconds: float = Field(ge=0)
    timed_out: bool = False


class BundleApplyResult(BundleApplyModel):
    bundle_id: str
    status: BundleApplyStatus
    applied_files: list[AppliedFileResult]
    changed_files: list[str]
    git_diff: str
    rollback_performed: bool
    rollback_succeeded: bool | None
    command_results: list[GitCommandResult]
    errors: list[str]
