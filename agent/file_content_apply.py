"""Safely replace one existing source file with validated complete content."""

from __future__ import annotations

import difflib
import hashlib
import os
import stat
import subprocess
from pathlib import Path

from agent.execution.errors import TaskExecutionError
from models.file_bundle_execution import FileBundleExecutionResult
from models.file_migration_execution import (
    FileContentApplyResult,
    FileContentApplyStatus,
)


class FileContentApplyError(TaskExecutionError):
    pass


def apply_file_content(
    repository: str | Path,
    execution: FileBundleExecutionResult,
    *,
    allow_dirty: bool = False,
    timeout_seconds: float = 30.0,
) -> FileContentApplyResult:
    root = Path(repository).expanduser().resolve()
    _validate_repository(root, timeout_seconds)
    relative = execution.file.replace("\\", "/")
    target = (root / relative).resolve()
    if root not in target.parents or not target.is_file():
        raise FileContentApplyError(
            "validate_target", f"target file does not exist: {relative}"
        )
    dirty = _git_status(root, timeout_seconds)
    if dirty and not allow_dirty:
        raise FileContentApplyError(
            "check_cleanliness",
            "repository has uncommitted changes",
            diagnostics={"dirty_files": ",".join(sorted(dirty))},
        )
    if relative in dirty:
        raise FileContentApplyError(
            "check_cleanliness",
            "target file already contains uncommitted changes",
            diagnostics={"dirty_files": relative},
        )

    original = target.read_bytes()
    original_hash = hashlib.sha256(original).hexdigest()
    if original_hash != execution.original_sha256:
        raise FileContentApplyError(
            "verify_original_content",
            "target file changed after model context was built",
            diagnostics={"file": relative},
        )
    updated_content = execution.proposal.updated_content
    if updated_content is None:
        raise FileContentApplyError(
            "validate_updated_content",
            "content_generated proposal has no updated_content",
        )
    updated = _encode_with_original_newlines(original, updated_content)
    if updated == original:
        raise FileContentApplyError(
            "validate_updated_content",
            "updated content is identical to current source",
        )
    mode = stat.S_IMODE(target.stat().st_mode)
    audit_diff = _audit_diff(relative, original, updated)
    try:
        target.write_bytes(updated)
        os.chmod(target, mode)
        if target.read_bytes() != updated:
            raise OSError("written content could not be verified")
        return FileContentApplyResult(
            bundle_id=execution.bundle_id,
            file=relative,
            status=FileContentApplyStatus.APPLIED,
            original_sha256=original_hash,
            updated_sha256=hashlib.sha256(updated).hexdigest(),
            audit_diff=audit_diff,
            errors=[],
        )
    except Exception as error:
        rollback_succeeded = _restore(target, original, mode)
        return FileContentApplyResult(
            bundle_id=execution.bundle_id,
            file=relative,
            status=(
                FileContentApplyStatus.ROLLED_BACK
                if rollback_succeeded
                else FileContentApplyStatus.FAILED
            ),
            original_sha256=original_hash,
            audit_diff=audit_diff,
            rollback_performed=True,
            rollback_succeeded=rollback_succeeded,
            errors=[f"{type(error).__name__}: {error}"],
        )


def _encode_with_original_newlines(original: bytes, content: str) -> bytes:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    newline = "\r\n" if b"\r\n" in original else "\n"
    return normalized.replace("\n", newline).encode("utf-8")


def _audit_diff(relative: str, original: bytes, updated: bytes) -> str:
    old = original.decode("utf-8").splitlines(keepends=True)
    new = updated.decode("utf-8").splitlines(keepends=True)
    return "".join(
        difflib.unified_diff(
            old,
            new,
            fromfile=f"a/{relative}",
            tofile=f"b/{relative}",
            n=3,
        )
    )


def _validate_repository(root: Path, timeout: float) -> None:
    if not root.is_dir():
        raise FileContentApplyError(
            "validate_repository", "repository is not a directory"
        )
    result = _run_git(root, ["rev-parse", "--show-toplevel"], timeout)
    if result.returncode != 0:
        raise FileContentApplyError(
            "validate_repository", "target is not a Git repository"
        )
    if Path(result.stdout.strip()).resolve() != root:
        raise FileContentApplyError(
            "validate_repository", "repository path must be the Git worktree root"
        )


def _git_status(root: Path, timeout: float) -> set[str]:
    result = _run_git(
        root,
        ["status", "--porcelain=v1", "-z", "--untracked-files=all"],
        timeout,
    )
    if result.returncode != 0:
        raise FileContentApplyError("git_status", "Git status failed")
    return {
        record[3:].replace("\\", "/")
        for record in result.stdout.split("\0")
        if len(record) >= 4
    }


def _run_git(
    root: Path, arguments: list[str], timeout: float
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=root,
            capture_output=True,
            text=True,
            check=False,
            shell=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise FileContentApplyError("run_git", "Git command timed out") from error
    except FileNotFoundError as error:
        raise FileContentApplyError(
            "run_git", "Git executable was not found"
        ) from error


def _restore(target: Path, content: bytes, mode: int) -> bool:
    try:
        target.write_bytes(content)
        os.chmod(target, mode)
        return target.read_bytes() == content
    except OSError:
        return False
