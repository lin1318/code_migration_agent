"""One-command orchestration of all planned Migration Bundles."""

from __future__ import annotations

import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path

from agent.bundle_apply import apply_bundle_patch
from agent.bundle_execution import execute_bundle, select_ready_bundle
from agent.bundle_validation import capture_validation_baseline
from knowledge.registry import RuleRegistry
from models.bundle_apply import BundleApplyStatus, PatchApplyRequest
from models.bundles import RepositoryMigrationPlan
from models.go_validation import CommandStatus, ValidationBaseline
from models.inspection import RepositoryInspection
from models.repository_migration import (
    BundleMigrationRecord,
    RepositoryMigrationRun,
    RepositoryMigrationStatus,
)
from tools.go_validation import (
    classify_diagnostics,
    parse_command_diagnostics,
    run_validation_command,
    skipped_command,
)


@dataclass(frozen=True)
class _Snapshot:
    existed: bool
    content: bytes
    mode: int


def migrate_repository(
    repository: str | Path,
    plan: RepositoryMigrationPlan,
    inspection: RepositoryInspection,
    registry: RuleRegistry,
    model_client,
    output_directory: str | Path,
    *,
    baseline: ValidationBaseline | None = None,
    dry_run: bool = False,
    timeout_seconds: float = 300.0,
) -> RepositoryMigrationRun:
    root = _clean_git_root(repository)
    output = Path(output_directory).expanduser().resolve()
    if output == root or root in output.parents:
        raise ValueError("migration output directory must be outside the repository")
    order = list(plan.ready_bundle_ordering)
    _validate_plan(plan, order)
    if dry_run:
        return _run_result(root, RepositoryMigrationStatus.PLANNED, order)
    output.mkdir(parents=True, exist_ok=True)
    baseline = baseline or capture_validation_baseline(root)
    baseline_path = output / "validation-baseline.json"
    baseline_path.write_text(baseline.model_dump_json(indent=2) + "\n", encoding="utf-8")
    snapshot_paths = sorted(
        {path for bundle in plan.bundles for path in bundle.files}
        | {"go.mod", "go.sum"}
    )
    snapshots = _snapshots(root, snapshot_paths)
    records: list[BundleMigrationRecord] = []
    completed: list[str] = []
    changed: set[str] = set()
    try:
        for index, bundle_id in enumerate(order, start=1):
            bundle = select_ready_bundle(
                plan,
                bundle_id,
                completed_bundles=completed,
            )
            result = execute_bundle(
                root,
                inspection,
                plan,
                bundle,
                registry,
                model_client,
                completed_bundles=completed,
            )
            proposal_path = output / f"{index:03d}-{bundle_id}-proposal.json"
            proposal_path.write_text(
                result.proposal.model_dump_json(indent=2) + "\n",
                encoding="utf-8",
            )
            if result.proposal.status.value == "no_change_needed":
                records.append(
                    BundleMigrationRecord(
                        bundle_id=bundle_id,
                        status="no_change_needed",
                        proposal_path=str(proposal_path),
                    )
                )
                completed.append(bundle_id)
                continue
            if result.proposal.status.value != "patch_generated":
                raise ValueError(
                    f"bundle {bundle_id} proposal status is "
                    f"{result.proposal.status.value!r}"
                )
            applied = apply_bundle_patch(
                PatchApplyRequest(
                    repository_path=str(root),
                    bundle=bundle,
                    proposal=result.proposal,
                    allow_dirty=bool(completed),
                    timeout_seconds=timeout_seconds,
                )
            )
            apply_path = output / f"{index:03d}-{bundle_id}-apply.json"
            apply_path.write_text(
                applied.model_dump_json(indent=2) + "\n",
                encoding="utf-8",
            )
            if applied.status != BundleApplyStatus.APPLIED:
                raise ValueError(
                    f"bundle {bundle_id} apply status is {applied.status.value!r}"
                )
            changed.update(applied.changed_files)
            records.append(
                BundleMigrationRecord(
                    bundle_id=bundle_id,
                    status="applied",
                    proposal_path=str(proposal_path),
                    apply_result_path=str(apply_path),
                )
            )
            completed.append(bundle_id)
    except Exception as error:
        rolled_back = _restore(root, snapshots)
        records.append(
            BundleMigrationRecord(
                bundle_id=order[len(completed)] if len(completed) < len(order) else "unknown",
                status="failed",
                error=f"{type(error).__name__}: {error}",
            )
        )
        return _run_result(
            root,
            RepositoryMigrationStatus.ROLLED_BACK if rolled_back else RepositoryMigrationStatus.FAILED,
            order,
            baseline=baseline,
            records=records,
            rollback_performed=True,
            rollback_succeeded=rolled_back,
            errors=[f"{type(error).__name__}: {error}"],
        )

    commands = []
    go_files = sorted(path for path in changed if path.endswith(".go"))
    if go_files:
        commands.append(
            run_validation_command(
                "gofmt",
                ["gofmt", "-w", *go_files],
                root,
                30.0,
            )
        )
    formatting_failed = bool(commands) and commands[-1].status != CommandStatus.PASSED
    commands.append(
        skipped_command("go_mod_tidy", root, "gofmt failed")
        if formatting_failed
        else run_validation_command(
            "go_mod_tidy", ["go", "mod", "tidy"], root, timeout_seconds
        )
    )
    dependency_changed = [
        path for path in ("go.mod", "go.sum") if path in _status(root)
    ]
    tidy_failed = commands[-1].status != CommandStatus.PASSED
    for operation, command in (
        ("go_build_repository", ["go", "build", "./..."]),
        ("go_test_repository", ["go", "test", "./..."]),
        ("go_vet_repository", ["go", "vet", "./..."]),
    ):
        commands.append(
            skipped_command(operation, root, "dependency reconciliation failed")
            if tidy_failed
            else run_validation_command(
                operation, command, root, timeout_seconds
            )
        )
    diagnostics = [
        diagnostic
        for index, command in enumerate(commands)
        for diagnostic in parse_command_diagnostics(command, index, root)
    ]
    introduced, persistent, resolved, unclassified = classify_diagnostics(
        diagnostics,
        baseline.diagnostics,
    )
    classifications = {
        item.normalized_key: item.classification
        for item in introduced + persistent + unclassified
    }
    diagnostics = [
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
    failed = any(
        command.status in {CommandStatus.FAILED, CommandStatus.TIMED_OUT}
        for command in commands
    )
    unexpected = sorted(
        set(_status(root)) - changed - {"go.mod", "go.sum"}
    )
    if introduced or (failed and not persistent) or unexpected:
        final_status = RepositoryMigrationStatus.FAILED
    elif persistent:
        final_status = RepositoryMigrationStatus.COMPLETED_WITH_PREEXISTING_FAILURES
    else:
        final_status = RepositoryMigrationStatus.COMPLETED
    run = _run_result(
        root,
        final_status,
        order,
        baseline=baseline,
        records=records,
        changed=sorted(changed),
        dependency_changed=dependency_changed,
        commands=commands,
        diagnostics=diagnostics,
        introduced=introduced,
        persistent=persistent,
        resolved=resolved,
        repair_required=final_status == RepositoryMigrationStatus.FAILED,
        errors=(
            [f"unexpected files changed: {unexpected}"] if unexpected else []
        ),
    )
    (output / "repository-migration-result.json").write_text(
        run.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
    return run


def _validate_plan(plan: RepositoryMigrationPlan, order: list[str]) -> None:
    ids = [bundle.bundle_id for bundle in plan.bundles]
    if len(ids) != len(set(ids)) or set(order) != set(ids):
        raise ValueError("migration plan bundle ordering is incomplete or duplicated")
    owners: dict[str, str] = {}
    for bundle in plan.bundles:
        for file in bundle.files:
            if file in owners:
                raise ValueError(
                    f"file {file!r} belongs to multiple bundles: "
                    f"{owners[file]}, {bundle.bundle_id}"
                )
            owners[file] = bundle.bundle_id


def _clean_git_root(repository: str | Path) -> Path:
    root = Path(repository).expanduser().resolve()
    top = _git(root, ["rev-parse", "--show-toplevel"]).strip()
    if not top or Path(top).resolve() != root:
        raise ValueError("repository path must be a Git worktree root")
    if _status(root):
        raise ValueError("repository must be clean before repository migration")
    return root


def _snapshots(root: Path, paths: list[str]) -> dict[str, _Snapshot]:
    result = {}
    for path in paths:
        target = root / path
        result[path] = _Snapshot(
            existed=target.is_file(),
            content=target.read_bytes() if target.is_file() else b"",
            mode=stat.S_IMODE(target.stat().st_mode) if target.is_file() else 0,
        )
    return result


def _restore(root: Path, snapshots: dict[str, _Snapshot]) -> bool:
    try:
        for path, snapshot in snapshots.items():
            target = root / path
            if snapshot.existed:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(snapshot.content)
                target.chmod(snapshot.mode)
            elif target.exists():
                target.unlink()
        return all(
            ((root / path).is_file() and (root / path).read_bytes() == item.content)
            if item.existed
            else not (root / path).exists()
            for path, item in snapshots.items()
        )
    except OSError:
        return False


def _status(root: Path) -> list[str]:
    output = _git(root, ["status", "--porcelain=v1", "-z"])
    return sorted(
        record[3:].replace("\\", "/")
        for record in output.split("\0")
        if len(record) >= 4
    )


def _git(root: Path, arguments: list[str]) -> str:
    completed = subprocess.run(
        ["git", *arguments],
        cwd=root,
        capture_output=True,
        text=True,
        shell=False,
        check=False,
    )
    if completed.returncode != 0:
        raise ValueError(completed.stderr.strip() or "Git command failed")
    return completed.stdout


def _run_result(
    root: Path,
    status: RepositoryMigrationStatus,
    order: list[str],
    *,
    baseline=None,
    records=None,
    changed=None,
    dependency_changed=None,
    commands=None,
    diagnostics=None,
    introduced=None,
    persistent=None,
    resolved=None,
    rollback_performed=False,
    rollback_succeeded=None,
    repair_required=False,
    errors=None,
) -> RepositoryMigrationRun:
    return RepositoryMigrationRun(
        repository_path=str(root),
        status=status,
        baseline=baseline,
        bundle_order=order,
        bundle_records=records or [],
        changed_files=changed or [],
        dependency_files_changed=dependency_changed or [],
        command_results=commands or [],
        diagnostics=diagnostics or [],
        introduced_diagnostics=introduced or [],
        persistent_diagnostics=persistent or [],
        resolved_diagnostics=resolved or [],
        rollback_performed=rollback_performed,
        rollback_succeeded=rollback_succeeded,
        repair_required=repair_required,
        errors=errors or [],
    )
