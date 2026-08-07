"""Execute and apply deterministic one-file bundles in their fixed order."""

from __future__ import annotations

from pathlib import Path

from agent.bundle_apply import BundleApplyError, apply_bundle_patch
from agent.execution.errors import ExecutionErrorDetail, TaskExecutionError
from agent.file_bundle_execution import FileBundleModelClient, execute_file_bundle
from knowledge.registry import RuleRegistry
from models.bundle_apply import BundleApplyStatus, PatchApplyRequest
from models.bundle_execution import (
    BundleFilePatch,
    BundlePatchProposal,
    BundlePatchStatus,
)
from models.bundles import BundleComplexity, MigrationBundle
from models.file_bundle_execution import FilePatchProposal, FilePatchStatus
from models.file_bundles import FileBundlePlan, FileMigrationBundle
from models.file_migration_execution import (
    FileBundleRunRecord,
    FileBundleRunStatus,
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
            if proposal.status is not FilePatchStatus.PATCH_GENERATED:
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

            application = apply_bundle_patch(
                PatchApplyRequest(
                    repository_path=str(root),
                    bundle=_application_bundle(bundle),
                    proposal=_application_proposal(execution.proposal),
                    allow_dirty=allow_dirty or has_applied_changes,
                    timeout_seconds=timeout_seconds,
                )
            )
            if application.status is BundleApplyStatus.APPLIED:
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
                if application.status is BundleApplyStatus.ROLLED_BACK
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


def _application_bundle(bundle: FileMigrationBundle) -> MigrationBundle:
    return MigrationBundle(
        bundle_id=bundle.bundle_id,
        files=[bundle.file],
        framework_symbols=bundle.framework_symbols,
        migration_concerns=[bundle.stage.value],
        external_dependencies=[],
        blocking_dependencies=[],
        depends_on=[],
        estimated_complexity=(
            BundleComplexity.MEDIUM
            if bundle.review_required
            else BundleComplexity.LOW
        ),
    )


def _application_proposal(proposal: FilePatchProposal) -> BundlePatchProposal:
    patch = proposal.patch
    if patch is None:
        raise BundleApplyError(
            "convert_file_proposal",
            "patch_generated proposal is missing its patch",
        )
    return BundlePatchProposal(
        bundle_id=proposal.bundle_id,
        status=BundlePatchStatus.PATCH_GENERATED,
        summary=proposal.summary,
        patches=[BundleFilePatch(file=proposal.file, patch=patch)],
        modified_files=[proposal.file],
        confidence=proposal.confidence,
        assumptions=proposal.assumptions,
        manual_review_notes=proposal.manual_review_notes,
        unresolved_dependencies=proposal.unresolved_dependencies,
    )


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
