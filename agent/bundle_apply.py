"""Safely apply exactly one validated Migration Bundle proposal."""

from __future__ import annotations

import os
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from agent.bundle_execution import validate_bundle_proposal
from agent.execution.errors import ProposalValidationError, TaskExecutionError
from models.bundle_apply import (
    AppliedFileResult,
    BundleApplyResult,
    BundleApplyStatus,
    GitCommandResult,
    PatchApplyRequest,
)
from models.bundle_execution import BundlePatchStatus


class BundleApplyError(TaskExecutionError):
    """A bundle cannot be safely validated or applied."""


@dataclass(frozen=True)
class _FileSnapshot:
    content: bytes
    mode: int


def apply_bundle_patch(request: PatchApplyRequest) -> BundleApplyResult:
    root = Path(request.repository_path).expanduser().resolve()
    command_results: list[GitCommandResult] = []
    _validate_repository(root, request.timeout_seconds, command_results)
    proposal = request.proposal
    if proposal.bundle_id != request.bundle.bundle_id:
        raise BundleApplyError(
            "validate_request",
            "proposal bundle_id does not match selected bundle",
        )
    if proposal.status == BundlePatchStatus.NO_CHANGE_NEEDED:
        validate_bundle_proposal(proposal, request.bundle)
        return _result(request, BundleApplyStatus.NO_CHANGE)
    if proposal.status != BundlePatchStatus.PATCH_GENERATED:
        status = (
            proposal.status.value
            if isinstance(proposal.status, BundlePatchStatus)
            else str(proposal.status)
        )
        raise BundleApplyError(
            "validate_status",
            f"proposal status {status!r} cannot be applied",
        )
    try:
        validate_bundle_proposal(proposal, request.bundle)
    except ProposalValidationError as error:
        raise BundleApplyError(
            "validate_proposal",
            error.details.message,
            diagnostics=error.details.diagnostics,
        ) from error

    affected = sorted(proposal.modified_files)
    targets = {path: _target_file(root, path) for path in affected}
    initial_status = _git_status(root, request.timeout_seconds, command_results)
    dirty = set(initial_status)
    if dirty and not request.allow_dirty:
        raise BundleApplyError(
            "check_cleanliness",
            "repository has uncommitted changes",
            diagnostics={"dirty_files": ",".join(sorted(dirty))},
        )
    dirty_targets = dirty.intersection(affected)
    if dirty_targets:
        raise BundleApplyError(
            "check_cleanliness",
            "proposed files already contain uncommitted changes",
            diagnostics={"dirty_files": ",".join(sorted(dirty_targets))},
        )

    snapshots = {
        path: _FileSnapshot(
            content=target.read_bytes(),
            mode=stat.S_IMODE(target.stat().st_mode),
        )
        for path, target in targets.items()
    }
    combined = "".join(
        _patch_with_target_line_endings(targets[item.file], item.patch)
        for item in proposal.patches
    )
    patch_path: Path | None = None
    mutated = False
    try:
        patch_path = _temporary_patch(combined)
        checked = _run_git(
            root,
            [
                "apply",
                "--check",
                "--unidiff-zero",
                "--ignore-space-change",
                str(patch_path),
            ],
            request.timeout_seconds,
        )
        command_results.append(checked)
        _require_success(checked, "dry_run")
        if request.dry_run:
            return _result(
                request,
                BundleApplyStatus.DRY_RUN_SUCCESS,
                command_results=command_results,
            )

        mutated = True
        applied = _run_git(
            root,
            [
                "apply",
                "--unidiff-zero",
                "--ignore-space-change",
                str(patch_path),
            ],
            request.timeout_seconds,
        )
        command_results.append(applied)
        _require_success(applied, "apply_patch")
        reverse_check = _run_git(
            root,
            [
                "apply",
                "--reverse",
                "--check",
                "--unidiff-zero",
                "--ignore-space-change",
                str(patch_path),
            ],
            request.timeout_seconds,
        )
        command_results.append(reverse_check)
        _require_success(reverse_check, "verify_patch")
        changed = _git_status(root, request.timeout_seconds, command_results)
        new_changes = set(changed) - dirty
        if new_changes != set(affected):
            raise BundleApplyError(
                "verify_changed_files",
                "changed files do not exactly match proposal.modified_files",
                diagnostics={
                    "expected": ",".join(affected),
                    "actual": ",".join(sorted(new_changes)),
                },
            )
        diff = _git_diff(root, affected, request.timeout_seconds, command_results)
        if not diff.strip():
            raise BundleApplyError("verify_diff", "applied Git diff is empty")
        files = _applied_file_results(
            root,
            affected,
            request.timeout_seconds,
            command_results,
        )
        return _result(
            request,
            BundleApplyStatus.APPLIED,
            applied_files=files,
            changed_files=affected,
            git_diff=diff,
            command_results=command_results,
        )
    except BundleApplyError as error:
        if not mutated:
            raise
        rollback_succeeded = _restore_and_verify(targets, snapshots)
        return _result(
            request,
            (
                BundleApplyStatus.ROLLED_BACK
                if rollback_succeeded
                else BundleApplyStatus.FAILED
            ),
            rollback_performed=True,
            rollback_succeeded=rollback_succeeded,
            command_results=command_results,
            errors=[error.details.message],
        )
    except Exception as error:
        if not mutated:
            raise BundleApplyError(
                "apply_bundle",
                f"unexpected patch application failure: {error}",
            ) from error
        rollback_succeeded = _restore_and_verify(targets, snapshots)
        return _result(
            request,
            (
                BundleApplyStatus.ROLLED_BACK
                if rollback_succeeded
                else BundleApplyStatus.FAILED
            ),
            rollback_performed=True,
            rollback_succeeded=rollback_succeeded,
            command_results=command_results,
            errors=[f"{type(error).__name__}: {error}"],
        )
    finally:
        if patch_path is not None:
            patch_path.unlink(missing_ok=True)


def _validate_repository(
    root: Path,
    timeout: float,
    results: list[GitCommandResult],
) -> None:
    if not root.exists() or not root.is_dir():
        raise BundleApplyError("validate_repository", "repository is not a directory")
    completed = _run_git(root, ["rev-parse", "--show-toplevel"], timeout)
    results.append(completed)
    if completed.timed_out:
        raise BundleApplyError("validate_repository", "Git command timed out")
    if completed.exit_code != 0:
        raise BundleApplyError("validate_repository", "target is not a Git repository")
    git_root = Path(completed.stdout.strip()).resolve()
    if git_root != root:
        raise BundleApplyError(
            "validate_repository",
            "repository path must be the Git worktree root",
            diagnostics={"git_root": str(git_root), "repository_path": str(root)},
        )


def _target_file(root: Path, relative: str) -> Path:
    target = (root / relative).resolve()
    if root not in target.parents or not target.is_file():
        raise BundleApplyError(
            "validate_target",
            f"target file does not exist: {relative}",
        )
    return target


def _temporary_patch(content: str) -> Path:
    descriptor, name = tempfile.mkstemp(prefix="migration-bundle-", suffix=".patch")
    path = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return path


def _patch_with_target_line_endings(target: Path, patch: str) -> str:
    content = target.read_bytes()
    newline = "\r\n" if b"\r\n" in content else "\n"
    return newline.join(patch.splitlines()) + newline


def _run_git(root: Path, arguments: list[str], timeout: float) -> GitCommandResult:
    command = ["git", *arguments]
    started = time.monotonic()
    try:
        completed = subprocess.run(
            command,
            cwd=root,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
            timeout=timeout,
        )
        return GitCommandResult(
            command=command,
            exit_code=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
            duration_seconds=time.monotonic() - started,
        )
    except subprocess.TimeoutExpired as error:
        return GitCommandResult(
            command=command,
            exit_code=None,
            stdout=_text(error.stdout),
            stderr=_text(error.stderr),
            duration_seconds=time.monotonic() - started,
            timed_out=True,
        )
    except FileNotFoundError as error:
        raise BundleApplyError("run_git", "Git executable was not found") from error


def _require_success(result: GitCommandResult, operation: str) -> None:
    if result.timed_out:
        raise BundleApplyError(operation, "Git command timed out")
    if result.exit_code != 0:
        message = result.stderr.strip() or result.stdout.strip() or "Git command failed"
        raise BundleApplyError(operation, message)


def _git_status(
    root: Path,
    timeout: float,
    results: list[GitCommandResult],
) -> dict[str, str]:
    result = _run_git(
        root,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        timeout,
    )
    results.append(result)
    _require_success(result, "git_status")
    status: dict[str, str] = {}
    records = result.stdout.split("\0")
    index = 0
    while index < len(records):
        line = records[index]
        index += 1
        if len(line) < 4:
            continue
        value = line[3:]
        status[value.replace("\\", "/")] = line[:2]
        if line[0] in "RC" or line[1] in "RC":
            index += 1
    return status


def _git_diff(
    root: Path,
    files: list[str],
    timeout: float,
    results: list[GitCommandResult],
) -> str:
    result = _run_git(root, ["diff", "--", *files], timeout)
    results.append(result)
    _require_success(result, "git_diff")
    return result.stdout


def _applied_file_results(
    root: Path,
    files: list[str],
    timeout: float,
    results: list[GitCommandResult],
) -> list[AppliedFileResult]:
    result = _run_git(root, ["diff", "--numstat", "--", *files], timeout)
    results.append(result)
    _require_success(result, "git_numstat")
    counts: dict[str, tuple[int, int]] = {}
    for line in result.stdout.splitlines():
        additions, deletions, path = line.split("\t", 2)
        counts[path.replace("\\", "/")] = (int(additions), int(deletions))
    return [
        AppliedFileResult(
            file=path,
            applied=True,
            additions=counts.get(path, (0, 0))[0],
            deletions=counts.get(path, (0, 0))[1],
        )
        for path in files
    ]


def _restore_and_verify(
    targets: dict[str, Path],
    snapshots: dict[str, _FileSnapshot],
) -> bool:
    try:
        for path, target in targets.items():
            snapshot = snapshots[path]
            target.write_bytes(snapshot.content)
            target.chmod(snapshot.mode)
        return all(
            target.read_bytes() == snapshots[path].content
            and stat.S_IMODE(target.stat().st_mode) == snapshots[path].mode
            for path, target in targets.items()
        )
    except OSError:
        return False


def _result(
    request: PatchApplyRequest,
    status: BundleApplyStatus,
    *,
    applied_files: list[AppliedFileResult] | None = None,
    changed_files: list[str] | None = None,
    git_diff: str = "",
    rollback_performed: bool = False,
    rollback_succeeded: bool | None = None,
    command_results: list[GitCommandResult] | None = None,
    errors: list[str] | None = None,
) -> BundleApplyResult:
    return BundleApplyResult(
        bundle_id=request.bundle.bundle_id,
        status=status,
        applied_files=applied_files or [],
        changed_files=changed_files or [],
        git_diff=git_diff,
        rollback_performed=rollback_performed,
        rollback_succeeded=rollback_succeeded,
        command_results=command_results or [],
        errors=errors or [],
    )


def _text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""
