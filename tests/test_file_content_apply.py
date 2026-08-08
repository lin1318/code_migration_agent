from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.file_content_apply import FileContentApplyError, apply_file_content
from models.file_bundle_execution import (
    FileBundleExecutionResult,
    FilePatchProposal,
    FilePatchStatus,
)
from models.file_migration_execution import FileContentApplyStatus
from models.patch_proposal import PatchConfidence


def _git(repository: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        shell=False,
    )


def _repository(tmp_path: Path, *, crlf: bool = False) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "tests@example.com")
    _git(repository, "config", "user.name", "Tests")
    content = b"package demo\r\n\r\nconst Old = true\r\n" if crlf else (
        b"package demo\n\nconst Old = true\n"
    )
    (repository / "main.go").write_bytes(content)
    _git(repository, "add", "main.go")
    _git(repository, "commit", "-m", "fixture")
    return repository


def _execution(repository: Path) -> FileBundleExecutionResult:
    original = (repository / "main.go").read_bytes()
    proposal = FilePatchProposal(
        bundle_id="file-bundle-main",
        file="main.go",
        status=FilePatchStatus.CONTENT_GENERATED,
        summary="update complete file",
        updated_content="package demo\n\nconst New = true\n",
        confidence=PatchConfidence.HIGH,
        assumptions=[],
        manual_review_notes=[],
        unresolved_dependencies=[],
    )
    return FileBundleExecutionResult(
        bundle_id=proposal.bundle_id,
        file=proposal.file,
        usage_count=1,
        migration_rule_count=1,
        read_only_symbol_count=0,
        prompt_size=100,
        model_used="fake",
        original_sha256=hashlib.sha256(original).hexdigest(),
        proposal=proposal,
    )


def test_replaces_complete_file_and_preserves_crlf(tmp_path: Path) -> None:
    repository = _repository(tmp_path, crlf=True)

    result = apply_file_content(repository, _execution(repository))

    assert result.status is FileContentApplyStatus.APPLIED
    assert b"const New = true\r\n" in (repository / "main.go").read_bytes()
    assert "--- a/main.go" in result.audit_diff
    assert "+++ b/main.go" in result.audit_diff


def test_rejects_file_changed_after_model_context(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    execution = _execution(repository)
    (repository / "main.go").write_text("package changed\n", encoding="utf-8")
    _git(repository, "add", "main.go")
    _git(repository, "commit", "-m", "concurrent change")

    with pytest.raises(FileContentApplyError, match="changed after model context"):
        apply_file_content(repository, execution)


def test_rejects_path_outside_repository(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    execution = _execution(repository).model_copy(update={"file": "../main.go"})

    with pytest.raises(FileContentApplyError, match="does not exist"):
        apply_file_content(repository, execution)


def test_write_failure_restores_original_file(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    target = repository / "main.go"
    original = target.read_bytes()

    with patch(
        "agent.file_content_apply.os.chmod",
        side_effect=[OSError("chmod failed"), None],
    ):
        result = apply_file_content(repository, _execution(repository))

    assert result.status is FileContentApplyStatus.ROLLED_BACK
    assert result.rollback_succeeded is True
    assert target.read_bytes() == original
