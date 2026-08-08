"""Safe source-file access shared by file-oriented migration."""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

from agent.execution.errors import ContextBuildError

MAX_SOURCE_FILE_SIZE = 1024 * 1024


def _repository_root(repository: str | os.PathLike[str]) -> Path:
    root = Path(repository).expanduser()
    if not root.exists():
        raise ContextBuildError(
            "validate_repository",
            f"repository path does not exist: {root}",
        )
    if not root.is_dir():
        raise ContextBuildError(
            "validate_repository",
            f"repository path is not a directory: {root}",
        )
    return root.resolve()


def _safe_relative_path(value: str) -> PurePosixPath:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or ".." in path.parts
        or any(part.casefold() == ".git" for part in path.parts)
        or (path.parts and path.parts[0].endswith(":"))
    ):
        raise ContextBuildError(
            "validate_source_path",
            f"unsafe repository-relative source path: {value}",
        )
    return path


def _read_source_file(root: Path, relative_path: str) -> str:
    safe_path = _safe_relative_path(relative_path)
    target = root.joinpath(*safe_path.parts).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError as error:
        raise ContextBuildError(
            "read_source_file",
            f"source path escapes repository: {relative_path}",
        ) from error
    if not target.is_file():
        raise ContextBuildError(
            "read_source_file",
            f"source file does not exist: {relative_path}",
        )
    if target.stat().st_size > MAX_SOURCE_FILE_SIZE:
        raise ContextBuildError(
            "read_source_file",
            f"source file exceeds {MAX_SOURCE_FILE_SIZE} bytes: {relative_path}",
        )
    data = target.read_bytes()
    if b"\x00" in data:
        raise ContextBuildError(
            "read_source_file",
            f"binary source file is not allowed: {relative_path}",
        )
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContextBuildError(
            "read_source_file",
            f"source file is not valid UTF-8: {relative_path}",
        ) from error
