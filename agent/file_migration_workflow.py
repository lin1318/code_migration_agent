"""Stage 6 orchestration for repository-facing file migration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel

from agent.file_bundle_execution import FileBundleModelClient
from agent.file_bundle_runner import run_file_migration
from knowledge.registry import load_rule_registry
from models.file_bundles import FileBundlePlan
from models.file_classification import GinFileClassification
from models.file_migration_execution import (
    FileBundleRunStatus,
    FileMigrationRunResult,
    FileMigrationRunStatus,
)
from models.file_migration_validation import (
    FileMigrationValidationResult,
    FileMigrationWorkflowReport,
    FileMigrationWorkflowStatus,
)
from models.file_migration_plan import FileMigrationPlan
from models.inspection import RepositoryInspection
from tools.file_bundle_planner import build_file_bundle_plan
from tools.file_classifier import classify_gin_files
from tools.file_migration_planner import build_file_migration_plan
from tools.inspection import DEFAULT_RULE_PATH, inspect_repository
from tools.file_migration_validation import validate_file_migration


@dataclass(frozen=True)
class FileMigrationWorkflowResult:
    inspection: RepositoryInspection
    classification: GinFileClassification
    file_plan: FileMigrationPlan
    bundle_plan: FileBundlePlan
    execution: FileMigrationRunResult
    status: FileMigrationWorkflowStatus
    validation: FileMigrationValidationResult | None
    output_directory: Path


def migrate_files(
    repository: str | Path,
    output_directory: str | Path,
    model_client: FileBundleModelClient,
    *,
    analyzer_timeout: float = 120.0,
    apply_timeout: float = 30.0,
    validation_timeout: float = 300.0,
    allow_dirty: bool = False,
    rule_path: str | Path = DEFAULT_RULE_PATH,
) -> FileMigrationWorkflowResult:
    """Connect Stages 1-5 and persist their deterministic artifacts."""

    repository_path = Path(repository).expanduser().resolve()
    output_path = Path(output_directory).expanduser().resolve()
    _validate_output_directory(repository_path, output_path)

    inspection = inspect_repository(
        repository_path,
        analyzer_timeout=analyzer_timeout,
        rule_path=rule_path,
    )
    registry = load_rule_registry(rule_path)
    classification = classify_gin_files(inspection)
    file_plan = build_file_migration_plan(classification)
    bundle_plan = build_file_bundle_plan(
        classification,
        file_plan,
        inspection,
        registry,
    )

    output_path.mkdir(parents=True, exist_ok=False)
    _write_json(output_path / "inspection.json", inspection)
    _write_json(output_path / "file-classification.json", classification)
    _write_json(output_path / "file-migration-plan.json", file_plan)
    _write_json(output_path / "file-bundle-plan.json", bundle_plan)

    execution = run_file_migration(
        repository_path,
        inspection,
        bundle_plan,
        registry,
        model_client,
        allow_dirty=allow_dirty,
        timeout_seconds=apply_timeout,
    )
    proposals = output_path / "proposals"
    proposals.mkdir()
    for record in execution.records:
        if record.execution is not None:
            _write_json(
                proposals / f"{record.bundle_id}.json",
                record.execution.proposal,
            )
    _write_json(output_path / "file-migration-result.json", execution)
    validation = None
    status = FileMigrationWorkflowStatus.MIGRATION_STOPPED
    if execution.status is FileMigrationRunStatus.COMPLETED:
        changed_files = [
            record.file
            for record in execution.records
            if record.status is FileBundleRunStatus.APPLIED
        ]
        validation = validate_file_migration(
            repository_path,
            changed_files,
            timeout_seconds=validation_timeout,
        )
        status = validation.status
        _write_json(output_path / "file-validation-result.json", validation)
    _write_json(
        output_path / "file-migration-workflow-result.json",
        FileMigrationWorkflowReport(
            repository_path=str(repository_path),
            status=status,
            migration=execution,
            validation=validation,
        ),
    )
    return FileMigrationWorkflowResult(
        inspection=inspection,
        classification=classification,
        file_plan=file_plan,
        bundle_plan=bundle_plan,
        execution=execution,
        status=status,
        validation=validation,
        output_directory=output_path,
    )


def _validate_output_directory(repository: Path, output: Path) -> None:
    if output.exists():
        raise ValueError(f"output directory already exists: {output}")
    if output == repository or repository in output.parents:
        raise ValueError(
            "output directory must be outside the migrated repository"
        )


def _write_json(path: Path, model: BaseModel) -> None:
    path.write_text(model.model_dump_json(indent=2) + "\n", encoding="utf-8")
