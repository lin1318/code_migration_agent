from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from models.go_validation import CommandStatus
from tools.go_validation import run_validation_command


def test_runs_allowlisted_command_without_shell(tmp_path: Path) -> None:
    completed = subprocess.CompletedProcess(
        ["go", "test", "./..."], 0, "ok", ""
    )
    with patch("tools.go_validation.subprocess.run", return_value=completed) as run:
        result = run_validation_command(
            "go_test", ["go", "test", "./..."], tmp_path, 12
        )

    assert result.status is CommandStatus.PASSED
    assert result.stdout == "ok"
    assert run.call_args.kwargs["shell"] is False
    assert run.call_args.kwargs["timeout"] == 12


def test_rejects_non_allowlisted_command(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not allowlisted"):
        run_validation_command("unsafe", ["powershell", "whoami"], tmp_path, 1)


def test_captures_timeout(tmp_path: Path) -> None:
    timeout = subprocess.TimeoutExpired(
        ["go", "vet", "./..."], 1, output=b"partial", stderr=b"slow"
    )
    with patch("tools.go_validation.subprocess.run", side_effect=timeout):
        result = run_validation_command(
            "go_vet", ["go", "vet", "./..."], tmp_path, 1
        )

    assert result.status is CommandStatus.TIMED_OUT
    assert result.stdout == "partial"
    assert result.stderr == "slow"
