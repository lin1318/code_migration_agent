"""Stage 11 validation for one applied Migration Bundle."""

from __future__ import annotations

import subprocess
from pathlib import Path

from models.bundle_apply import BundleApplyResult, BundleApplyStatus
from models.bundle_execution import BundlePatchProposal
from models.bundles import MigrationBundle
from models.go_validation import (
    BundleValidationResult,
    BundleValidationStatus,
    CommandStatus,
    GoCommandResult,
    GoDiagnostic,
    PackageValidationResult,
    ValidationBaseline,
    ValidationLevel,
)
from tools.go_validation import (
    classify_diagnostics,
    parse_command_diagnostics,
    run_validation_command,
    skipped_command,
    validation_summary,
)

DEFAULT_TIMEOUTS = {
    "gofmt": 30.0,
    "goimports": 30.0,
    "go_mod_tidy": 180.0,
    "go_build_affected": 120.0,
    "go_test_affected": 180.0,
    "go_build_repository": 300.0,
    "go_test_repository": 600.0,
    "go_vet_repository": 300.0,
}


def capture_validation_baseline(
    repository: str | Path,
    *,
    timeout_seconds: float | None = None,
) -> ValidationBaseline:
    root = _git_root(repository)
    before = _status(root)
    commands = [
        _run(
            "go_build_repository",
            ["go", "build", "./..."],
            root,
            timeout_seconds,
        ),
        _run(
            "go_test_repository",
            ["go", "test", "./..."],
            root,
            timeout_seconds,
        ),
        _run(
            "go_vet_repository",
            ["go", "vet", "./..."],
            root,
            timeout_seconds,
        ),
    ]
    after = _status(root)
    if after != before:
        raise ValueError("baseline validation unexpectedly modified the repository")
    diagnostics = _diagnostics(commands, root)
    revision = _git_text(root, ["rev-parse", "HEAD"])
    return ValidationBaseline(
        repository_revision=revision.strip(),
        repository_status=sorted(before),
        command_results=commands,
        diagnostics=diagnostics,
        package_results=_package_results([], commands, diagnostics),
        summary=validation_summary(commands, diagnostics),
    )


def validate_applied_bundle(
    repository: str | Path,
    bundle: MigrationBundle,
    proposal: BundlePatchProposal,
    apply_result: BundleApplyResult,
    *,
    baseline: ValidationBaseline | None = None,
    level: ValidationLevel = ValidationLevel.FULL,
    fail_fast: bool = False,
    use_goimports: bool = False,
    run_mod_tidy: bool = False,
    timeout_seconds: float | None = None,
) -> BundleValidationResult:
    root = _git_root(repository)
    state_error = _applied_state_error(root, bundle, proposal, apply_result)
    if state_error:
        return _empty_result(
            root,
            bundle.bundle_id,
            level,
            BundleValidationStatus.INVALID_STATE,
            state_error,
        )

    modified_go = sorted(path for path in proposal.modified_files if path.endswith(".go"))
    affected_packages = _affected_packages(modified_go)
    dependency_bundle = bool(
        {"go.mod", "go.sum"}.intersection(proposal.modified_files)
        or "dependencies" in bundle.migration_concerns
    )
    if run_mod_tidy and not dependency_bundle:
        return _empty_result(
            root,
            bundle.bundle_id,
            level,
            BundleValidationStatus.INVALID_STATE,
            "go mod tidy is allowed only for an explicit dependency bundle",
        )

    commands: list[GoCommandResult] = []
    formatted_files: list[str] = []
    dependency_files_changed: list[str] = []
    initial_status = set(_status(root))
    allowed_mutations = set(proposal.modified_files)
    stop = False

    if level in {ValidationLevel.FORMAT, ValidationLevel.FULL}:
        gofmt = _run(
            "gofmt",
            ["gofmt", "-w", *modified_go],
            root,
            timeout_seconds,
        ) if modified_go else skipped_command("gofmt", root, "no modified Go files")
        commands.append(gofmt)
        if gofmt.status == CommandStatus.PASSED:
            formatted_files = modified_go
            stop = not _mutations_within(root, initial_status, allowed_mutations)
        elif not gofmt.skipped:
            stop = True
        if use_goimports:
            command = (
                _run(
                    "goimports",
                    ["goimports", "-w", *modified_go],
                    root,
                    timeout_seconds,
                )
                if modified_go and not stop
                else skipped_command("goimports", root, "formatting did not complete")
            )
            commands.append(command)
            if command.status != CommandStatus.PASSED and not command.skipped:
                stop = True
        else:
            commands.append(skipped_command("goimports", root, "goimports disabled"))
    else:
        commands.extend(
            [
                skipped_command("gofmt", root, f"not included in {level.value} level"),
                skipped_command("goimports", root, "goimports disabled"),
            ]
        )

    if run_mod_tidy and not stop:
        before_dependency = {
            path: (root / path).read_bytes() if (root / path).is_file() else None
            for path in ("go.mod", "go.sum")
        }
        tidy = _run("go_mod_tidy", ["go", "mod", "tidy"], root, timeout_seconds)
        commands.append(tidy)
        after_dependency = set(_status(root))
        dependency_files_changed = sorted(
            path
            for path, content in before_dependency.items()
            if ((root / path).read_bytes() if (root / path).is_file() else None)
            != content
        )
        allowed_mutations.update({"go.mod", "go.sum"})
        if not after_dependency.issubset(allowed_mutations):
            stop = True
        if tidy.status != CommandStatus.PASSED:
            stop = True
    else:
        reason = "earlier required operation failed" if stop else "go mod tidy not requested"
        commands.append(skipped_command("go_mod_tidy", root, reason))

    affected_enabled = level in {ValidationLevel.AFFECTED, ValidationLevel.FULL}
    affected_targets = affected_packages or ["."]
    affected_build = _conditional_run(
        "go_build_affected",
        ["go", "build", *affected_targets],
        root,
        affected_enabled and not stop,
        "affected validation disabled or earlier operation failed",
        timeout_seconds,
    )
    commands.append(affected_build)
    affected_test = _conditional_run(
        "go_test_affected",
        ["go", "test", *affected_targets],
        root,
        affected_enabled
        and not stop
        and affected_build.status == CommandStatus.PASSED,
        "affected build did not pass",
        timeout_seconds,
    )
    commands.append(affected_test)
    if fail_fast and any(
        item.status in {CommandStatus.FAILED, CommandStatus.TIMED_OUT}
        for item in (affected_build, affected_test)
    ):
        stop = True

    repository_enabled = level in {ValidationLevel.REPOSITORY, ValidationLevel.FULL}
    repository_build = _conditional_run(
        "go_build_repository",
        ["go", "build", "./..."],
        root,
        repository_enabled and not stop,
        "repository validation disabled or fail-fast stopped execution",
        timeout_seconds,
    )
    commands.append(repository_build)
    repository_test = _conditional_run(
        "go_test_repository",
        ["go", "test", "./..."],
        root,
        repository_enabled
        and not stop
        and (repository_build.status == CommandStatus.PASSED or not fail_fast),
        "repository build failed and fail-fast is enabled",
        timeout_seconds,
    )
    commands.append(repository_test)
    if fail_fast and any(
        item.status in {CommandStatus.FAILED, CommandStatus.TIMED_OUT}
        for item in (repository_build, repository_test)
    ):
        stop = True
    vet = _conditional_run(
        "go_vet_repository",
        ["go", "vet", "./..."],
        root,
        repository_enabled and not stop,
        "repository validation disabled or fail-fast stopped execution",
        timeout_seconds,
    )
    commands.append(vet)

    diagnostics = _diagnostics(commands, root)
    introduced, persistent, resolved, unclassified = classify_diagnostics(
        diagnostics,
        baseline.diagnostics if baseline is not None else None,
    )
    classifications = {
        item.normalized_key: item.classification
        for item in introduced + persistent + unclassified
    }
    classified = [
        item.model_copy(
            update={
                "classification": classifications.get(
                    item.normalized_key,
                    item.classification,
                )
            }
        )
        for item in diagnostics
    ]
    timed_out = any(item.timed_out and not item.skipped for item in commands)
    failed = any(
        item.status == CommandStatus.FAILED
        for item in commands
        if not item.skipped
    )
    mutation_valid = _mutations_within(root, initial_status, allowed_mutations)
    if timed_out:
        status = BundleValidationStatus.TIMED_OUT
    elif not mutation_valid or introduced or (failed and not persistent):
        status = BundleValidationStatus.FAILED
    elif persistent:
        status = BundleValidationStatus.PASSED_WITH_PREEXISTING_FAILURES
    else:
        status = BundleValidationStatus.PASSED
    repository_diff = _git_text(root, ["diff"])
    package_results = _package_results(affected_packages, commands, classified)
    return BundleValidationResult(
        bundle_id=bundle.bundle_id,
        status=status,
        repository_path=str(root),
        validation_level=level,
        affected_packages=affected_packages,
        formatted_files=formatted_files,
        dependency_files_changed=dependency_files_changed,
        command_results=commands,
        diagnostics=classified,
        introduced_diagnostics=introduced,
        persistent_diagnostics=persistent,
        resolved_diagnostics=resolved,
        unclassified_diagnostics=unclassified,
        package_results=package_results,
        repository_diff=repository_diff,
        summary=validation_summary(commands, classified),
        repair_required=status in {
            BundleValidationStatus.FAILED,
            BundleValidationStatus.TIMED_OUT,
        },
    )


def _applied_state_error(
    root: Path,
    bundle: MigrationBundle,
    proposal: BundlePatchProposal,
    result: BundleApplyResult,
) -> str | None:
    if result.status != BundleApplyStatus.APPLIED:
        status = (
            result.status.value
            if isinstance(result.status, BundleApplyStatus)
            else str(result.status)
        )
        return f"bundle apply result status is {status!r}, not 'applied'"
    if result.bundle_id != bundle.bundle_id or proposal.bundle_id != bundle.bundle_id:
        return "bundle, proposal, and apply-result IDs do not match"
    expected = sorted(proposal.modified_files)
    if sorted(result.changed_files) != expected:
        return "apply-result changed_files do not match proposal.modified_files"
    if any(not (root / path).is_file() for path in expected):
        return "one or more expected modified files are missing"
    if set(_status(root)) != set(expected):
        return "repository state no longer matches the Stage 10 apply result"
    return None


def _affected_packages(files: list[str]) -> list[str]:
    targets = {
        "." if Path(path).parent.as_posix() == "." else f"./{Path(path).parent.as_posix()}"
        for path in files
    }
    return sorted(targets)


def _run(
    operation: str,
    command: list[str],
    root: Path,
    timeout: float | None,
) -> GoCommandResult:
    return run_validation_command(
        operation,
        command,
        root,
        timeout or DEFAULT_TIMEOUTS[operation],
    )


def _conditional_run(
    operation: str,
    command: list[str],
    root: Path,
    enabled: bool,
    reason: str,
    timeout: float | None,
) -> GoCommandResult:
    return _run(operation, command, root, timeout) if enabled else skipped_command(
        operation, root, reason
    )


def _diagnostics(commands: list[GoCommandResult], root: Path) -> list[GoDiagnostic]:
    return [
        diagnostic
        for index, command in enumerate(commands)
        for diagnostic in parse_command_diagnostics(command, index, root)
    ]


def _package_results(
    packages: list[str],
    commands: list[GoCommandResult],
    diagnostics: list[GoDiagnostic],
) -> list[PackageValidationResult]:
    by_operation = {item.operation: item.status for item in commands}
    return [
        PackageValidationResult(
            package=package,
            build_status=by_operation.get("go_build_affected", CommandStatus.SKIPPED),
            test_status=by_operation.get("go_test_affected", CommandStatus.SKIPPED),
            diagnostics=[
                item for item in diagnostics if item.package in {None, package}
            ],
        )
        for package in packages
    ]


def _mutations_within(root: Path, initial: set[str], allowed: set[str]) -> bool:
    current = set(_status(root))
    return current.issubset(initial.union(allowed))


def _empty_result(
    root: Path,
    bundle_id: str,
    level: ValidationLevel,
    status: BundleValidationStatus,
    message: str,
) -> BundleValidationResult:
    command = skipped_command("validate_applied_state", root, message)
    return BundleValidationResult(
        bundle_id=bundle_id,
        status=status,
        repository_path=str(root),
        validation_level=level,
        affected_packages=[],
        formatted_files=[],
        dependency_files_changed=[],
        command_results=[command],
        diagnostics=[],
        introduced_diagnostics=[],
        persistent_diagnostics=[],
        resolved_diagnostics=[],
        unclassified_diagnostics=[],
        package_results=[],
        repository_diff="",
        summary=validation_summary([command], []),
        repair_required=False,
    )


def _git_root(repository: str | Path) -> Path:
    root = Path(repository).expanduser().resolve()
    if not root.is_dir():
        raise ValueError("repository is not a directory")
    value = _git_text(root, ["rev-parse", "--show-toplevel"]).strip()
    if not value or Path(value).resolve() != root:
        raise ValueError("repository path must be the Git worktree root")
    return root


def _status(root: Path) -> list[str]:
    output = _git_text(root, ["status", "--porcelain=v1", "-z"])
    records = output.split("\0")
    paths: list[str] = []
    index = 0
    while index < len(records):
        record = records[index]
        index += 1
        if len(record) < 4:
            continue
        paths.append(record[3:].replace("\\", "/"))
        if record[0] in "RC" or record[1] in "RC":
            index += 1
    return sorted(paths)


def _git_text(root: Path, arguments: list[str]) -> str:
    try:
        completed = subprocess.run(
            ["git", *arguments],
            cwd=root,
            capture_output=True,
            text=True,
            shell=False,
            check=False,
        )
    except FileNotFoundError as error:
        raise ValueError("Git executable was not found") from error
    if completed.returncode != 0:
        raise ValueError(completed.stderr.strip() or "Git command failed")
    return completed.stdout
