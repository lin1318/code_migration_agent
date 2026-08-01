from __future__ import annotations

import json
import stat
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
import agent.bundle_apply as bundle_apply_module
from agent.bundle_apply import BundleApplyError, apply_bundle_patch
from agent.cli import main
from models.bundle_apply import BundleApplyStatus, GitCommandResult, PatchApplyRequest
from models.bundle_execution import BundleFilePatch, BundlePatchProposal
from models.bundles import BundleComplexity, MigrationBundle


@pytest.fixture
def git_repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "tests@example.com")
    _git(repository, "config", "user.name", "Tests")
    (repository / "main.go").write_text(
        'package main\n\nfunc main() {\n\tprintln("old")\n}\n',
        encoding="utf-8",
    )
    (repository / "unrelated.txt").write_text("original\n", encoding="utf-8")
    _git(repository, "add", "main.go", "unrelated.txt")
    _git(repository, "commit", "-m", "fixture")
    return repository


@pytest.fixture
def bundle() -> MigrationBundle:
    return MigrationBundle(
        bundle_id="migration-bundle-test",
        files=["main.go"],
        framework_symbols=["github.com/gin-gonic/gin.Default"],
        migration_concerns=["server_bootstrap"],
        external_dependencies=[],
        blocking_dependencies=[],
        depends_on=[],
        estimated_complexity=BundleComplexity.LOW,
    )


@pytest.fixture
def proposal(bundle: MigrationBundle) -> BundlePatchProposal:
    return _proposal(bundle.bundle_id)


def _proposal(bundle_id: str) -> BundlePatchProposal:
    patch_text = (
        "diff --git a/main.go b/main.go\n"
        "--- a/main.go\n"
        "+++ b/main.go\n"
        "@@ -1,5 +1,5 @@\n"
        " package main\n"
        " \n"
        " func main() {\n"
        '-\tprintln("old")\n'
        '+\tprintln("new")\n'
        " }\n"
    )
    return BundlePatchProposal(
        bundle_id=bundle_id,
        status="patch_generated",
        summary="Update main.",
        patches=[BundleFilePatch(file="main.go", patch=patch_text)],
        modified_files=["main.go"],
        confidence="high",
        assumptions=[],
        manual_review_notes=[],
        unresolved_dependencies=[],
    )


def _request(
    repository: Path,
    bundle: MigrationBundle,
    proposal: BundlePatchProposal,
    **changes,
) -> PatchApplyRequest:
    return PatchApplyRequest(
        repository_path=str(repository),
        bundle=bundle,
        proposal=proposal,
        **changes,
    )


def _git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
        shell=False,
    )


def test_valid_dry_run_does_not_mutate_repository(
    git_repository: Path,
    bundle: MigrationBundle,
    proposal: BundlePatchProposal,
) -> None:
    before = (git_repository / "main.go").read_bytes()

    result = apply_bundle_patch(
        _request(git_repository, bundle, proposal, dry_run=True)
    )

    assert result.status == BundleApplyStatus.DRY_RUN_SUCCESS
    assert result.changed_files == []
    assert result.command_results[-1].command[1:3] == ["apply", "--check"]
    assert (git_repository / "main.go").read_bytes() == before
    assert _git(git_repository, "status", "--porcelain").stdout == ""


def test_valid_proposal_applies_and_returns_exact_diff(
    git_repository: Path,
    bundle: MigrationBundle,
    proposal: BundlePatchProposal,
) -> None:
    result = apply_bundle_patch(_request(git_repository, bundle, proposal))

    assert result.status == BundleApplyStatus.APPLIED
    assert result.changed_files == ["main.go"]
    assert [item.file for item in result.applied_files] == ["main.go"]
    assert result.applied_files[0].additions == 1
    assert result.applied_files[0].deletions == 1
    assert 'println("new")' in (git_repository / "main.go").read_text()
    assert "diff --git a/main.go b/main.go" in result.git_diff
    assert "unrelated.txt" not in result.git_diff


def test_lf_model_patch_applies_to_crlf_target(
    git_repository: Path,
    bundle: MigrationBundle,
    proposal: BundlePatchProposal,
) -> None:
    target = git_repository / "main.go"
    target.write_bytes(target.read_bytes().replace(b"\n", b"\r\n"))
    _git(git_repository, "add", "main.go")
    _git(git_repository, "commit", "-m", "CRLF target")
    assert b"\r\n" in target.read_bytes()

    result = apply_bundle_patch(_request(git_repository, bundle, proposal))

    assert result.status == BundleApplyStatus.APPLIED
    assert b'println("new")' in target.read_bytes()
    assert b"\r\n" in target.read_bytes()


@pytest.mark.parametrize("status", ["blocked", "failed"])
def test_non_applicable_status_is_rejected(
    git_repository: Path,
    bundle: MigrationBundle,
    proposal: BundlePatchProposal,
    status: str,
) -> None:
    invalid = proposal.model_copy(
        update={"status": status, "patches": [], "modified_files": []}
    )
    with pytest.raises(BundleApplyError, match="cannot be applied"):
        apply_bundle_patch(_request(git_repository, bundle, invalid))


def test_no_change_needed_returns_noop(
    git_repository: Path,
    bundle: MigrationBundle,
    proposal: BundlePatchProposal,
) -> None:
    unchanged = proposal.model_copy(
        update={
            "status": "no_change_needed",
            "patches": [],
            "modified_files": [],
        }
    )
    result = apply_bundle_patch(_request(git_repository, bundle, unchanged))
    assert result.status == BundleApplyStatus.NO_CHANGE
    assert result.command_results == []


def test_inconsistent_modified_files_is_rejected(
    git_repository: Path,
    bundle: MigrationBundle,
    proposal: BundlePatchProposal,
) -> None:
    invalid = proposal.model_copy(update={"modified_files": []})
    with pytest.raises(BundleApplyError, match="exactly match"):
        apply_bundle_patch(_request(git_repository, bundle, invalid))


@pytest.mark.parametrize(
    ("file", "patch_text", "message"),
    [
        (
            "outside.go",
            "diff --git a/outside.go b/outside.go\n"
            "--- a/outside.go\n+++ b/outside.go\n@@ -1 +1 @@\n-old\n+new\n",
            "outside the bundle",
        ),
        (
            "/main.go",
            "diff --git a//main.go b//main.go\n"
            "--- a//main.go\n+++ b//main.go\n@@ -1 +1 @@\n-old\n+new\n",
            "unsafe patch path",
        ),
        (
            "../main.go",
            "diff --git a/../main.go b/../main.go\n"
            "--- a/../main.go\n+++ b/../main.go\n@@ -1 +1 @@\n-old\n+new\n",
            "unsafe patch path",
        ),
        (
            ".git/config",
            "diff --git a/.git/config b/.git/config\n"
            "--- a/.git/config\n+++ b/.git/config\n@@ -1 +1 @@\n-old\n+new\n",
            "unsafe patch path",
        ),
        (
            "main.go",
            "diff --git a/main.go b/main.go\nGIT binary patch\n",
            "binary patches",
        ),
        (
            "main.go",
            "diff --git a/main.go b/main.go\nnew file mode 100644\n"
            "--- /dev/null\n+++ b/main.go\n@@ -0,0 +1 @@\n+new\n",
            "new-file",
        ),
        (
            "main.go",
            "diff --git a/main.go b/main.go\ndeleted file mode 100644\n"
            "--- a/main.go\n+++ /dev/null\n@@ -1 +0,0 @@\n-old\n",
            "delete-file",
        ),
        (
            "main.go",
            "diff --git a/main.go b/renamed.go\nsimilarity index 100%\n"
            "rename from main.go\nrename to renamed.go\n",
            "rename",
        ),
        (
            "main.go",
            "not a patch",
            "diff --git",
        ),
    ],
)
def test_unsafe_patch_forms_are_rejected_before_mutation(
    git_repository: Path,
    bundle: MigrationBundle,
    proposal: BundlePatchProposal,
    file: str,
    patch_text: str,
    message: str,
) -> None:
    if file == "outside.go":
        changed_bundle = bundle
    else:
        changed_bundle = bundle.model_copy(update={"files": [file]})
    invalid = proposal.model_copy(
        update={
            "patches": [BundleFilePatch(file=file, patch=patch_text)],
            "modified_files": [file],
        }
    )
    before = (git_repository / "main.go").read_bytes()
    with pytest.raises(BundleApplyError, match=message):
        apply_bundle_patch(_request(git_repository, changed_bundle, invalid))
    assert (git_repository / "main.go").read_bytes() == before


def test_duplicate_patch_and_missing_target_are_rejected(
    git_repository: Path,
    bundle: MigrationBundle,
    proposal: BundlePatchProposal,
) -> None:
    duplicate = proposal.model_copy(
        update={"patches": [proposal.patches[0], proposal.patches[0]]}
    )
    with pytest.raises(BundleApplyError, match="duplicate modifications"):
        apply_bundle_patch(_request(git_repository, bundle, duplicate))

    missing_bundle = bundle.model_copy(update={"files": ["missing.go"]})
    missing_patch = proposal.patches[0].patch.replace("main.go", "missing.go")
    missing = proposal.model_copy(
        update={
            "patches": [
                proposal.patches[0].model_copy(
                    update={"file": "missing.go", "patch": missing_patch}
                )
            ],
            "modified_files": ["missing.go"],
        }
    )
    with pytest.raises(BundleApplyError, match="does not exist"):
        apply_bundle_patch(_request(git_repository, missing_bundle, missing))


def test_dirty_repository_policy_preserves_unrelated_changes(
    git_repository: Path,
    bundle: MigrationBundle,
    proposal: BundlePatchProposal,
) -> None:
    unrelated = git_repository / "unrelated.txt"
    unrelated.write_text("user change\n", encoding="utf-8")
    with pytest.raises(BundleApplyError, match="uncommitted changes"):
        apply_bundle_patch(_request(git_repository, bundle, proposal))

    result = apply_bundle_patch(
        _request(git_repository, bundle, proposal, allow_dirty=True)
    )
    assert result.status == BundleApplyStatus.APPLIED
    assert unrelated.read_text(encoding="utf-8") == "user change\n"
    assert result.changed_files == ["main.go"]


def test_allow_dirty_rejects_dirty_target(
    git_repository: Path,
    bundle: MigrationBundle,
    proposal: BundlePatchProposal,
) -> None:
    (git_repository / "main.go").write_text("user change\n", encoding="utf-8")
    with pytest.raises(BundleApplyError, match="proposed files"):
        apply_bundle_patch(
            _request(git_repository, bundle, proposal, allow_dirty=True)
        )


def test_non_git_repository_is_rejected(
    tmp_path: Path,
    bundle: MigrationBundle,
    proposal: BundlePatchProposal,
) -> None:
    (tmp_path / "main.go").write_text("package main\n", encoding="utf-8")
    with pytest.raises(BundleApplyError, match="not a Git repository"):
        apply_bundle_patch(_request(tmp_path, bundle, proposal))


def test_git_subdirectory_is_rejected_as_repository_root(
    git_repository: Path,
    bundle: MigrationBundle,
    proposal: BundlePatchProposal,
) -> None:
    subdirectory = git_repository / "nested"
    subdirectory.mkdir()
    (subdirectory / "main.go").write_text("package main\n", encoding="utf-8")

    with pytest.raises(BundleApplyError, match="Git worktree root"):
        apply_bundle_patch(_request(subdirectory, bundle, proposal))


def test_git_apply_check_failure_leaves_repository_unchanged(
    git_repository: Path,
    bundle: MigrationBundle,
    proposal: BundlePatchProposal,
) -> None:
    bad_patch = proposal.patches[0].patch.replace('println("old")', 'println("missing")')
    invalid = proposal.model_copy(
        update={"patches": [BundleFilePatch(file="main.go", patch=bad_patch)]}
    )
    before = (git_repository / "main.go").read_bytes()
    with pytest.raises(BundleApplyError, match="patch failed"):
        apply_bundle_patch(_request(git_repository, bundle, invalid))
    assert (git_repository / "main.go").read_bytes() == before


def test_post_apply_failure_rolls_back_bytes_and_mode(
    git_repository: Path,
    bundle: MigrationBundle,
    proposal: BundlePatchProposal,
) -> None:
    target = git_repository / "main.go"
    before = target.read_bytes()
    before_mode = stat.S_IMODE(target.stat().st_mode)
    real_run = bundle_apply_module._run_git

    def fail_reverse(root, arguments, timeout):
        if arguments[:3] == ["apply", "--reverse", "--check"]:
            return GitCommandResult(
                command=["git", *arguments],
                exit_code=1,
                stdout="",
                stderr="forced verification failure",
                duration_seconds=0,
            )
        return real_run(root, arguments, timeout)

    with patch("agent.bundle_apply._run_git", side_effect=fail_reverse):
        result = apply_bundle_patch(_request(git_repository, bundle, proposal))

    assert result.status == BundleApplyStatus.ROLLED_BACK
    assert result.rollback_performed is True
    assert result.rollback_succeeded is True
    assert target.read_bytes() == before
    assert stat.S_IMODE(target.stat().st_mode) == before_mode
    assert _git(git_repository, "status", "--porcelain").stdout == ""


def test_apply_command_failure_rolls_back_partial_mutation(
    git_repository: Path,
    bundle: MigrationBundle,
    proposal: BundlePatchProposal,
) -> None:
    target = git_repository / "main.go"
    unrelated = git_repository / "unrelated.txt"
    before = target.read_bytes()
    unrelated_before = unrelated.read_bytes()
    real_run = bundle_apply_module._run_git

    def fail_apply(root, arguments, timeout):
        if arguments[0] == "apply" and "--check" not in arguments:
            target.write_text("partial mutation\n", encoding="utf-8")
            return GitCommandResult(
                command=["git", *arguments],
                exit_code=1,
                stdout="",
                stderr="forced apply failure",
                duration_seconds=0,
            )
        return real_run(root, arguments, timeout)

    with patch("agent.bundle_apply._run_git", side_effect=fail_apply):
        result = apply_bundle_patch(_request(git_repository, bundle, proposal))

    assert result.status == BundleApplyStatus.ROLLED_BACK
    assert result.rollback_succeeded is True
    assert target.read_bytes() == before
    assert unrelated.read_bytes() == unrelated_before


def test_dry_run_structured_result_is_deterministic_where_applicable(
    git_repository: Path,
    bundle: MigrationBundle,
    proposal: BundlePatchProposal,
) -> None:
    request = _request(git_repository, bundle, proposal, dry_run=True)

    first = apply_bundle_patch(request).model_dump()
    second = apply_bundle_patch(request).model_dump()
    for result in (first, second):
        result["command_results"] = []

    assert first == second


def test_git_timeout_is_structured_and_repository_unchanged(
    git_repository: Path,
    bundle: MigrationBundle,
    proposal: BundlePatchProposal,
) -> None:
    before = (git_repository / "main.go").read_bytes()
    timeout_result = GitCommandResult(
        command=["git", "status"],
        exit_code=None,
        stdout="",
        stderr="",
        duration_seconds=0,
        timed_out=True,
    )
    with patch("agent.bundle_apply._run_git", return_value=timeout_result):
        with pytest.raises(BundleApplyError, match="timed out"):
            apply_bundle_patch(_request(git_repository, bundle, proposal))
    assert (git_repository / "main.go").read_bytes() == before


def test_cli_loads_bundle_and_proposal_without_model_call(
    git_repository: Path,
    bundle: MigrationBundle,
    proposal: BundlePatchProposal,
    tmp_path: Path,
) -> None:
    bundle_path = tmp_path / "bundle.json"
    proposal_path = tmp_path / "proposal.json"
    output = tmp_path / "result.json"
    bundle_path.write_text(bundle.model_dump_json(), encoding="utf-8")
    proposal_path.write_text(proposal.model_dump_json(), encoding="utf-8")

    exit_code = main(
        [
            "bundle",
            "apply",
            "--repo",
            str(git_repository),
            "--bundle",
            str(bundle_path),
            "--proposal",
            str(proposal_path),
            "--dry-run",
            "--output",
            str(output),
        ]
    )

    assert exit_code == 0
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == (
        "dry_run_success"
    )
    assert not any(path.suffix == ".patch" for path in git_repository.rglob("*"))
