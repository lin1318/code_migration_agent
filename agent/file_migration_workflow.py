"""Stage 6 orchestration for repository-facing file migration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from langgraph.checkpoint.base import BaseCheckpointSaver
from pydantic import BaseModel

from agent.file_bundle_execution import FileBundleModelClient
from models.file_bundles import FileBundlePlan
from models.file_classification import GinFileClassification
from models.file_migration_execution import FileMigrationRunResult
from models.file_migration_validation import (
    FileMigrationValidationResult,
    FileMigrationWorkflowStatus,
)
from models.file_migration_plan import FileMigrationPlan
from models.inspection import RepositoryInspection
from tools.inspection import DEFAULT_RULE_PATH
from workflow.file_service import run_file_migration_workflow


@dataclass(frozen=True)
class FileMigrationWorkflowResult:
    inspection: RepositoryInspection
    classification: GinFileClassification
    file_plan: FileMigrationPlan
    bundle_plan: FileBundlePlan
    execution: FileMigrationRunResult
    status: FileMigrationWorkflowStatus
    validation: FileMigrationValidationResult | None
    completed_stages: list[str]
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
    checkpointer: BaseCheckpointSaver | None = None,
    thread_id: str = "workflow-migrate-files",
) -> FileMigrationWorkflowResult:
    """Run and persist the LangGraph-managed file migration workflow."""

    repository_path = Path(repository).expanduser().resolve()
    output_path = Path(output_directory).expanduser().resolve()
    state = run_file_migration_workflow(
        repository_path,
        output_path,
        rule_path,
        model_client,
        analyzer_timeout=analyzer_timeout,
        apply_timeout=apply_timeout,
        validation_timeout=validation_timeout,
        allow_dirty=allow_dirty,
        checkpointer=checkpointer,
        thread_id=thread_id,
    )
    if state.get("final_status") == "failed":
        errors = state.get("errors", [])
        detail = errors[-1] if errors else None
        message = (
            f"{detail.stage}: {detail.error_type}: {detail.message}"
            if detail is not None
            else "file migration workflow failed"
        )
        raise RuntimeError(message)
    inspection = state["inspection"]
    classification = state["classification"]
    file_plan = state["file_plan"]
    bundle_plan = state["bundle_plan"]
    execution = state["execution"]
    validation = state.get("validation")
    report = state["final_report"]

    _write_json(output_path / "inspection.json", inspection)
    _write_json(output_path / "file-classification.json", classification)
    _write_json(output_path / "file-migration-plan.json", file_plan)
    _write_json(output_path / "file-bundle-plan.json", bundle_plan)

    proposals = output_path / "proposals"
    proposals.mkdir()
    for record in execution.records:
        if record.execution is not None:
            _write_json(
                proposals / f"{record.bundle_id}.json",
                record.execution.proposal,
            )
    _write_json(output_path / "file-migration-result.json", execution)
    if validation is not None:
        _write_json(output_path / "file-validation-result.json", validation)
    _write_json(
        output_path / "file-migration-workflow-result.json",
        report,
    )
    return FileMigrationWorkflowResult(
        inspection=inspection,
        classification=classification,
        file_plan=file_plan,
        bundle_plan=bundle_plan,
        execution=execution,
        status=report.status,
        validation=validation,
        completed_stages=state.get("completed_stages", []),
        output_directory=output_path,
    )


def _write_json(path: Path, model: BaseModel) -> None:
    path.write_text(model.model_dump_json(indent=2) + "\n", encoding="utf-8")
