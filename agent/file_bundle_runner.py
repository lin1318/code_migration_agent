"""Execute and apply deterministic one-file bundles in their fixed order."""

from __future__ import annotations

from pathlib import Path

from agent.execution.errors import ExecutionErrorDetail, TaskExecutionError
from agent.file_bundle_execution import FileBundleModelClient, execute_file_bundle
from agent.file_content_apply import apply_file_content
from knowledge.registry import RuleRegistry
from models.file_bundle_execution import FilePatchStatus
from models.file_bundles import FileBundlePlan, FileMigrationBundle
from models.file_migration_execution import (
    FileBundleRunRecord,
    FileBundleRunStatus,
    FileContentApplyStatus,
    FileMigrationRunResult,
    FileMigrationRunStatus,
)
from models.inspection import RepositoryInspection


def run_file_migration(
    repository: str | Path,
    inspection: RepositoryInspection,
    plan: FileBundlePlan,
    registry: RuleRegistry,
    model_client: FileBundleModelClient,
    *,
    allow_dirty: bool = False,
    timeout_seconds: float = 30.0,
) -> FileMigrationRunResult:
    """Generate and apply each file bundle, stopping at the first failure."""

    root = Path(repository).expanduser().resolve()
    _validate_plan_order(plan)
    completed_files: list[str] = []
    records: list[FileBundleRunRecord] = []
    has_applied_changes = False

    for bundle in plan.bundles:
        try:
            execution = execute_file_bundle(
                root,
                inspection,
                bundle,
                registry,
                model_client,
                completed_files=completed_files,
            )
            proposal = execution.proposal
            if proposal.status is FilePatchStatus.NO_CHANGE_NEEDED:
                completed_files.append(bundle.file)
                records.append(
                    FileBundleRunRecord(
                        bundle_id=bundle.bundle_id,
                        file=bundle.file,
                        status=FileBundleRunStatus.NO_CHANGE,
                        execution=execution,
                    )
                )
                continue
            if proposal.status is not FilePatchStatus.CONTENT_GENERATED:
                record_status = (
                    FileBundleRunStatus.BLOCKED
                    if proposal.status is FilePatchStatus.BLOCKED
                    else FileBundleRunStatus.FAILED
                )
                records.append(
                    FileBundleRunRecord(
                        bundle_id=bundle.bundle_id,
                        file=bundle.file,
                        status=record_status,
                        execution=execution,
                        error=ExecutionErrorDetail(
                            operation="execute_file_bundle",
                            error_type=(
                                "FileBundleBlockedError"
                                if record_status is FileBundleRunStatus.BLOCKED
                                else "FileBundleGenerationError"
                            ),
                            message=proposal.summary,
                            diagnostics={
                                "unresolved_dependencies": ",".join(
                                    proposal.unresolved_dependencies
                                )
                            },
                        ),
                    )
                )
                return _stopped(root, plan, completed_files, records, bundle)

            application = apply_file_content(
                root,
                execution,
                allow_dirty=allow_dirty or has_applied_changes,
                timeout_seconds=timeout_seconds,
            )
            if application.status is FileContentApplyStatus.APPLIED:
                has_applied_changes = True
                completed_files.append(bundle.file)
                records.append(
                    FileBundleRunRecord(
                        bundle_id=bundle.bundle_id,
                        file=bundle.file,
                        status=FileBundleRunStatus.APPLIED,
                        execution=execution,
                        application=application,
                    )
                )
                continue
            status = (
                FileBundleRunStatus.ROLLED_BACK
                if application.status is FileContentApplyStatus.ROLLED_BACK
                else FileBundleRunStatus.FAILED
            )
            records.append(
                FileBundleRunRecord(
                    bundle_id=bundle.bundle_id,
                    file=bundle.file,
                    status=status,
                    execution=execution,
                    application=application,
                    error=ExecutionErrorDetail(
                        operation="apply_file_bundle",
                        error_type="BundleApplyError",
                        message="; ".join(application.errors)
                        or f"unexpected apply status: {application.status.value}",
                        diagnostics={},
                    ),
                )
            )
            return _stopped(root, plan, completed_files, records, bundle)
        except TaskExecutionError as error:
            records.append(_error_record(bundle, error.details))
            return _stopped(root, plan, completed_files, records, bundle)
        except Exception as error:
            detail = ExecutionErrorDetail(
                operation="run_file_migration",
                error_type=type(error).__name__,
                message=str(error),
                diagnostics={},
            )
            records.append(_error_record(bundle, detail))
            return _stopped(root, plan, completed_files, records, bundle)

    return FileMigrationRunResult(
        repository_path=str(root),
        status=FileMigrationRunStatus.COMPLETED,
        bundle_count=len(plan.bundles),
        completed_files=completed_files,
        records=records,
    )


def _validate_plan_order(plan: FileBundlePlan) -> None:
    bundle_ids = [bundle.bundle_id for bundle in plan.bundles]
    if plan.bundle_ordering != bundle_ids:
        raise ValueError("bundle_ordering must match the bundle execution order")
    if [bundle.order for bundle in plan.bundles] != list(
        range(1, len(plan.bundles) + 1)
    ):
        raise ValueError("bundle order values must be consecutive")


def _error_record(
    bundle: FileMigrationBundle,
    detail: ExecutionErrorDetail,
) -> FileBundleRunRecord:
    return FileBundleRunRecord(
        bundle_id=bundle.bundle_id,
        file=bundle.file,
        status=FileBundleRunStatus.FAILED,
        error=detail,
    )


def _stopped(
    root: Path,
    plan: FileBundlePlan,
    completed_files: list[str],
    records: list[FileBundleRunRecord],
    failed_bundle: FileMigrationBundle,
) -> FileMigrationRunResult:
    return FileMigrationRunResult(
        repository_path=str(root),
        status=FileMigrationRunStatus.STOPPED,
        bundle_count=len(plan.bundles),
        completed_files=completed_files,
        records=records,
        failed_bundle_id=failed_bundle.bundle_id,
    )
