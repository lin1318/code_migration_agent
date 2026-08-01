from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.bundle_apply import apply_bundle_patch
from agent.cli import main
from agent.bundle_validation import (
    capture_validation_baseline,
    validate_applied_bundle,
)
from models.bundle_apply import BundleApplyStatus, PatchApplyRequest
from models.bundle_execution import BundleFilePatch, BundlePatchProposal
from models.bundles import BundleComplexity, MigrationBundle
from models.go_validation import (
    BundleValidationStatus,
    DiagnosticCategory,
    DiagnosticClassification,
    GoCommandResult,
    ValidationLevel,
)
from tools.go_validation import (
    classify_diagnostics,
    parse_command_diagnostics,
    run_validation_command,
)


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
        shell=False,
    ).stdout


def _repository(tmp_path: Path, *, source: str | None = None) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "validation@example.com")
    _git(repository, "config", "user.name", "Validation Tests")
    (repository / "go.mod").write_text("module example.com/sample\n\ngo 1.23\n")
    (repository / "main.go").write_text(
        source
        or "package sample\n\nfunc Message() string {\n\treturn \"old\"\n}\n"
    )
    (repository / "main_test.go").write_text(
        "package sample\n\nimport \"testing\"\n\n"
        "func TestMessage(t *testing.T) {\n"
        '\tif Message() != "old" { t.Fatal("unexpected message") }\n'
        "}\n"
    )
    (repository / "unrelated.go").write_text(
        "package sample\n\nfunc Unrelated( )int{return 1}\n"
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "fixture")
    return repository


def _bundle() -> MigrationBundle:
    return MigrationBundle(
        bundle_id="migration-bundle-validation",
        files=["main.go"],
        framework_symbols=["github.com/gin-gonic/gin.Default"],
        migration_concerns=["server_bootstrap"],
        external_dependencies=[],
        blocking_dependencies=[],
        depends_on=[],
        estimated_complexity=BundleComplexity.LOW,
    )


def _proposal(old: str, new: str) -> BundlePatchProposal:
    patch_text = (
        "diff --git a/main.go b/main.go\n"
        "--- a/main.go\n"
        "+++ b/main.go\n"
        "@@ -1,5 +1,5 @@\n"
        " package sample\n"
        " \n"
        " func Message() string {\n"
        f"-\t{old}\n"
        f"+\t{new}\n"
        " }\n"
    )
    return BundlePatchProposal(
        bundle_id="migration-bundle-validation",
        status="patch_generated",
        summary="Fixture migration.",
        patches=[BundleFilePatch(file="main.go", patch=patch_text)],
        modified_files=["main.go"],
        confidence="high",
        assumptions=[],
        manual_review_notes=[],
        unresolved_dependencies=[],
    )


def _apply(repository: Path, proposal: BundlePatchProposal):
    bundle = _bundle()
    result = apply_bundle_patch(
        PatchApplyRequest(
            repository_path=str(repository),
            bundle=bundle,
            proposal=proposal,
        )
    )
    assert result.status == BundleApplyStatus.APPLIED
    return bundle, result


def _command(
    operation: str,
    *,
    exit_code: int | None = 1,
    stdout: str = "",
    stderr: str = "",
    timed_out: bool = False,
) -> GoCommandResult:
    return GoCommandResult(
        operation=operation,
        command=["go", "test", "./..."],
        working_directory="C:/repo",
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=0,
        timed_out=timed_out,
        started="2026-01-01T00:00:00+00:00",
        completed="2026-01-01T00:00:00+00:00",
    )


def _passed(operation: str) -> GoCommandResult:
    return _command(operation, exit_code=0)


def test_diagnostic_parser_compiler_test_vet_and_unknown(tmp_path: Path) -> None:
    repository = tmp_path.resolve()
    compiler = parse_command_diagnostics(
        _command(
            "go_build_repository",
            stderr=f"{repository / 'main.go'}:14:25: undefined: gin.Context\n",
        ),
        0,
        repository,
    )[0]
    test = parse_command_diagnostics(
        _command("go_test_repository", stdout="--- FAIL: TestSomething (0.01s)\n"),
        1,
        repository,
    )[0]
    vet = parse_command_diagnostics(
        _command("go_vet_repository", stderr="main.go:3:2: unreachable code\n"),
        2,
        repository,
    )[0]
    unknown = parse_command_diagnostics(
        _command("go_build_repository", stderr="malformed compiler output\n"),
        3,
        repository,
    )[0]

    assert compiler.category == DiagnosticCategory.COMPILER_ERROR
    assert compiler.file == "main.go"
    assert (compiler.line, compiler.column) == (14, 25)
    assert test.category == DiagnosticCategory.TEST_FAILURE
    assert test.test_name == "TestSomething"
    assert vet.category == DiagnosticCategory.VET_ERROR
    assert unknown.raw_line == "malformed compiler output"
    assert compiler.diagnostic_id == parse_command_diagnostics(
        _command(
            "go_build_repository",
            stderr="main.go:14:25: undefined: gin.Context\n",
        ),
        9,
        repository,
    )[0].diagnostic_id


def test_timeout_and_missing_executable_are_structured(tmp_path: Path) -> None:
    with patch(
        "tools.go_validation.subprocess.run",
        side_effect=subprocess.TimeoutExpired(["go", "test"], 1),
    ):
        timeout = run_validation_command(
            "go_test_repository", ["go", "test", "./..."], tmp_path, 1
        )
    assert timeout.timed_out is True
    assert parse_command_diagnostics(timeout, 0, tmp_path)[0].category == (
        DiagnosticCategory.COMMAND_TIMEOUT
    )

    with patch("tools.go_validation.subprocess.run", side_effect=FileNotFoundError("go")):
        missing = run_validation_command(
            "go_build_repository", ["go", "build", "./..."], tmp_path, 1
        )
    assert missing.exit_code is None
    assert "go" in missing.stderr
    assert parse_command_diagnostics(missing, 0, tmp_path)[0].category == (
        DiagnosticCategory.COMMAND_EXECUTION_ERROR
    )


def test_baseline_classification_introduced_persistent_resolved_unclassified(
    tmp_path: Path,
) -> None:
    first = parse_command_diagnostics(
        _command("go_build_repository", stderr="main.go:2:3: undefined: Old\n"),
        0,
        tmp_path,
    )[0]
    second = parse_command_diagnostics(
        _command("go_build_repository", stderr="main.go:4:5: undefined: New\n"),
        0,
        tmp_path,
    )[0]

    introduced, persistent, resolved, unclassified = classify_diagnostics(
        [first, second], [first]
    )
    assert [item.classification for item in introduced] == [
        DiagnosticClassification.INTRODUCED
    ]
    assert [item.classification for item in persistent] == [
        DiagnosticClassification.PERSISTENT
    ]
    assert resolved == []
    assert unclassified == []
    _, _, resolved, _ = classify_diagnostics([], [first])
    assert resolved[0].classification == DiagnosticClassification.RESOLVED
    assert classify_diagnostics([second], None)[3][0].classification == (
        DiagnosticClassification.UNCLASSIFIED
    )


def test_baseline_capture_is_non_mutating(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    before = {
        path.relative_to(repository).as_posix(): path.read_bytes()
        for path in repository.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }

    baseline = capture_validation_baseline(repository)

    after = {
        path.relative_to(repository).as_posix(): path.read_bytes()
        for path in repository.rglob("*")
        if path.is_file() and ".git" not in path.parts
    }
    assert baseline.summary.failed_command_count == 0
    assert before == after
    assert _git(repository, "status", "--porcelain") == ""


def test_full_validation_formats_only_modified_file_and_passes(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    baseline = capture_validation_baseline(repository)
    proposal = _proposal('return "old"', 'return  "old" // migrated')
    bundle, apply_result = _apply(repository, proposal)
    unrelated_before = (repository / "unrelated.go").read_bytes()

    result = validate_applied_bundle(
        repository,
        bundle,
        proposal,
        apply_result,
        baseline=baseline,
    )

    assert result.status == BundleValidationStatus.PASSED
    assert result.affected_packages == ["."]
    assert result.formatted_files == ["main.go"]
    assert (repository / "unrelated.go").read_bytes() == unrelated_before
    assert "diff --git a/main.go b/main.go" in result.repository_diff
    assert result.repair_required is False


@pytest.mark.parametrize("status", ["dry_run_success", "rolled_back", "failed"])
def test_unapplied_results_are_invalid_state(tmp_path: Path, status: str) -> None:
    repository = _repository(tmp_path)
    proposal = _proposal('return "old"', 'return  "old"')
    bundle, applied = _apply(repository, proposal)
    invalid = applied.model_copy(update={"status": status})

    result = validate_applied_bundle(repository, bundle, proposal, invalid)

    assert result.status == BundleValidationStatus.INVALID_STATE


def test_repository_state_mismatch_is_invalid(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    proposal = _proposal('return "old"', 'return  "old"')
    bundle, applied = _apply(repository, proposal)
    (repository / "main_test.go").write_text("unexpected\n")

    result = validate_applied_bundle(repository, bundle, proposal, applied)

    assert result.status == BundleValidationStatus.INVALID_STATE


def test_introduced_compiler_failure_is_failed(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    baseline = capture_validation_baseline(repository)
    proposal = _proposal('return "old"', "return MissingSymbol")
    bundle, applied = _apply(repository, proposal)

    result = validate_applied_bundle(
        repository, bundle, proposal, applied, baseline=baseline
    )

    assert result.status == BundleValidationStatus.FAILED
    assert result.introduced_diagnostics
    assert any(
        item.category == DiagnosticCategory.COMPILER_ERROR
        for item in result.introduced_diagnostics
    )
    assert result.repair_required is True


def test_introduced_test_failure_is_failed(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    baseline = capture_validation_baseline(repository)
    proposal = _proposal('return "old"', 'return "new"')
    bundle, applied = _apply(repository, proposal)

    result = validate_applied_bundle(
        repository, bundle, proposal, applied, baseline=baseline
    )

    assert result.status == BundleValidationStatus.FAILED
    assert any(
        item.category == DiagnosticCategory.TEST_FAILURE
        for item in result.introduced_diagnostics
    )


def test_persistent_preexisting_failure_is_not_attributed(tmp_path: Path) -> None:
    repository = _repository(
        tmp_path,
        source=(
            "package sample\n\nfunc Message() string {\n"
            "\t_ = MissingSymbol\n\treturn \"old\"\n}\n"
        ),
    )
    baseline = capture_validation_baseline(repository)
    patch_text = (
        "diff --git a/main.go b/main.go\n--- a/main.go\n+++ b/main.go\n"
        "@@ -3,4 +3,4 @@\n func Message() string {\n"
        " \t_ = MissingSymbol\n-\treturn \"old\"\n+\treturn  \"old\"\n }\n"
    )
    proposal = _proposal('return "old"', 'return  "old"').model_copy(
        update={"patches": [BundleFilePatch(file="main.go", patch=patch_text)]}
    )
    bundle, applied = _apply(repository, proposal)

    result = validate_applied_bundle(
        repository, bundle, proposal, applied, baseline=baseline
    )

    assert result.status == BundleValidationStatus.PASSED_WITH_PREEXISTING_FAILURES
    assert result.persistent_diagnostics
    assert not result.introduced_diagnostics


def test_affected_packages_are_deduplicated_and_ordered() -> None:
    from agent.bundle_validation import _affected_packages

    assert _affected_packages(
        ["routers/z.go", "middleware/jwt/a.go", "routers/a.go"]
    ) == ["./middleware/jwt", "./routers"]


def test_mod_tidy_rejected_for_non_dependency_bundle(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    proposal = _proposal('return "old"', 'return  "old"')
    bundle, applied = _apply(repository, proposal)

    result = validate_applied_bundle(
        repository,
        bundle,
        proposal,
        applied,
        run_mod_tidy=True,
    )

    assert result.status == BundleValidationStatus.INVALID_STATE


def test_gofmt_failure_stops_later_commands(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    proposal = _proposal('return "old"', 'return  "old"')
    bundle, applied = _apply(repository, proposal)
    failure = _command("gofmt", stderr="main.go:4: formatting failed")

    with patch("agent.bundle_validation.run_validation_command", return_value=failure):
        result = validate_applied_bundle(repository, bundle, proposal, applied)

    assert result.status == BundleValidationStatus.FAILED
    assert result.command_results[0].operation == "gofmt"
    assert all(item.skipped for item in result.command_results[1:])


def test_no_baseline_failure_is_unclassified_not_introduced(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    proposal = _proposal('return "old"', "return MissingSymbol")
    bundle, applied = _apply(repository, proposal)

    result = validate_applied_bundle(repository, bundle, proposal, applied)

    assert result.status == BundleValidationStatus.FAILED
    assert result.introduced_diagnostics == []
    assert result.unclassified_diagnostics


def test_format_level_and_optional_goimports_routing(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    proposal = _proposal('return "old"', 'return  "old" // migrated')
    bundle, applied = _apply(repository, proposal)

    def pass_command(operation, command, root, timeout):
        del command, root, timeout
        return _passed(operation)

    with patch(
        "agent.bundle_validation.run_validation_command",
        side_effect=pass_command,
    ):
        result = validate_applied_bundle(
            repository,
            bundle,
            proposal,
            applied,
            level=ValidationLevel.FORMAT,
            use_goimports=True,
        )

    by_operation = {item.operation: item for item in result.command_results}
    assert by_operation["gofmt"].status.value == "passed"
    assert by_operation["goimports"].status.value == "passed"
    assert by_operation["go_build_affected"].skipped is True
    assert by_operation["go_build_affected"].skip_reason


def test_affected_build_failure_skips_test(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    proposal = _proposal('return "old"', 'return "new"')
    bundle, applied = _apply(repository, proposal)

    def commands(operation, command, root, timeout):
        del command, root, timeout
        if operation == "go_build_affected":
            return _command(operation, stderr="main.go:4:2: undefined: Broken")
        return _passed(operation)

    with patch(
        "agent.bundle_validation.run_validation_command",
        side_effect=commands,
    ):
        result = validate_applied_bundle(
            repository,
            bundle,
            proposal,
            applied,
            level=ValidationLevel.AFFECTED,
        )

    by_operation = {item.operation: item for item in result.command_results}
    assert result.status == BundleValidationStatus.FAILED
    assert by_operation["go_test_affected"].skipped is True
    assert by_operation["go_test_affected"].skip_reason == "affected build did not pass"


def test_repository_build_failure_continues_test_and_vet_by_default(
    tmp_path: Path,
) -> None:
    repository = _repository(tmp_path)
    proposal = _proposal('return "old"', 'return "new"')
    bundle, applied = _apply(repository, proposal)

    def commands(operation, command, root, timeout):
        del command, root, timeout
        if operation == "go_build_repository":
            return _command(operation, stderr="main.go:4:2: undefined: Broken")
        return _passed(operation)

    with patch(
        "agent.bundle_validation.run_validation_command",
        side_effect=commands,
    ):
        result = validate_applied_bundle(
            repository,
            bundle,
            proposal,
            applied,
            level=ValidationLevel.REPOSITORY,
        )

    by_operation = {item.operation: item for item in result.command_results}
    assert by_operation["go_test_repository"].status.value == "passed"
    assert by_operation["go_vet_repository"].status.value == "passed"


def test_required_command_timeout_sets_timed_out_status(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    proposal = _proposal('return "old"', 'return  "old" // migrated')
    bundle, applied = _apply(repository, proposal)

    with patch(
        "agent.bundle_validation.run_validation_command",
        return_value=_command("gofmt", exit_code=None, timed_out=True),
    ):
        result = validate_applied_bundle(repository, bundle, proposal, applied)

    assert result.status == BundleValidationStatus.TIMED_OUT
    assert result.repair_required is True


def test_unexpected_formatter_mutation_is_rejected(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    proposal = _proposal('return "old"', 'return  "old" // migrated')
    bundle, applied = _apply(repository, proposal)

    def mutate(operation, command, root, timeout):
        del command, timeout
        if operation == "gofmt":
            (root / "unrelated.go").write_text("package sample\n// changed\n")
        return _passed(operation)

    with patch(
        "agent.bundle_validation.run_validation_command",
        side_effect=mutate,
    ):
        result = validate_applied_bundle(repository, bundle, proposal, applied)

    assert result.status == BundleValidationStatus.FAILED


def test_dependency_bundle_may_run_mod_tidy(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    proposal = _proposal('return "old"', 'return  "old" // migrated')
    bundle, applied = _apply(repository, proposal)
    dependency_bundle = bundle.model_copy(
        update={"migration_concerns": ["dependencies"]}
    )

    result = validate_applied_bundle(
        repository,
        dependency_bundle,
        proposal,
        applied,
        level=ValidationLevel.FORMAT,
        run_mod_tidy=True,
    )

    by_operation = {item.operation: item for item in result.command_results}
    assert by_operation["go_mod_tidy"].status.value == "passed"
    assert set(result.dependency_files_changed).issubset({"go.mod", "go.sum"})


def test_resolved_baseline_failure_is_reported(tmp_path: Path) -> None:
    repository = _repository(
        tmp_path,
        source=(
            "package sample\n\nfunc Message() string {\n"
            "\t_ = MissingSymbol\n\treturn \"old\"\n}\n"
        ),
    )
    baseline = capture_validation_baseline(repository)
    patch_text = (
        "diff --git a/main.go b/main.go\n--- a/main.go\n+++ b/main.go\n"
        "@@ -3,4 +3,4 @@\n func Message() string {\n"
        "-\t_ = MissingSymbol\n+\t_ = 1\n"
        " \treturn \"old\"\n }\n"
    )
    proposal = _proposal('return "old"', 'return "old"').model_copy(
        update={"patches": [BundleFilePatch(file="main.go", patch=patch_text)]}
    )
    bundle, applied = _apply(repository, proposal)

    result = validate_applied_bundle(
        repository, bundle, proposal, applied, baseline=baseline
    )

    assert result.status == BundleValidationStatus.PASSED
    assert result.resolved_diagnostics


def test_baseline_and_bundle_validation_cli_do_not_call_model(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    baseline_path = tmp_path / "baseline.json"
    assert main(
        [
            "validate",
            "baseline",
            "--repo",
            str(repository),
            "--output",
            str(baseline_path),
        ],
        stdout=io.StringIO(),
        stderr=io.StringIO(),
    ) == 0
    proposal = _proposal('return "old"', 'return  "old" // migrated')
    bundle, applied = _apply(repository, proposal)
    bundle_path = tmp_path / "bundle.json"
    proposal_path = tmp_path / "proposal.json"
    apply_path = tmp_path / "apply.json"
    output_path = tmp_path / "validation.json"
    bundle_path.write_text(bundle.model_dump_json(), encoding="utf-8")
    proposal_path.write_text(proposal.model_dump_json(), encoding="utf-8")
    apply_path.write_text(applied.model_dump_json(), encoding="utf-8")

    with patch("agent.bundle_cli.create_bundle_model_client") as create_model:
        exit_code = main(
            [
                "bundle",
                "validate",
                "--repo",
                str(repository),
                "--bundle",
                str(bundle_path),
                "--proposal",
                str(proposal_path),
                "--apply-result",
                str(apply_path),
                "--baseline",
                str(baseline_path),
                "--level",
                "full",
                "--output",
                str(output_path),
            ],
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )

    assert exit_code == 0
    assert json.loads(output_path.read_text(encoding="utf-8"))["status"] == "passed"
    create_model.assert_not_called()
