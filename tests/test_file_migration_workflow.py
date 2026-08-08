from __future__ import annotations

from io import StringIO
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from agent.cli import main
from agent.file_migration_workflow import (
    FileMigrationWorkflowResult,
    migrate_files,
)
from knowledge.registry import load_rule_registry
from models.analyzer import AnalyzerResult, GinUsage
from models.file_bundle_execution import (
    FileBundleExecutionResult,
    FilePatchProposal,
    FilePatchStatus,
)
from models.file_migration_execution import (
    FileBundleRunRecord,
    FileBundleRunStatus,
    FileMigrationRunResult,
    FileMigrationRunStatus,
)
from models.file_migration_validation import (
    FileMigrationValidationResult,
    FileMigrationWorkflowStatus,
)
from models.inspection import RepositoryInspection
from models.patch_proposal import PatchConfidence
from tools.file_bundle_planner import build_file_bundle_plan
from tools.file_classifier import classify_gin_files
from tools.file_migration_planner import build_file_migration_plan
from tools.inspection import DEFAULT_RULE_PATH
from tools.migration_planner import build_migration_inventory

GIN = "github.com/gin-gonic/gin"


def _inspection(repository: Path) -> RepositoryInspection:
    analysis = AnalyzerResult(
        module_path="example.com/app",
        packages=[],
        usages=[
            GinUsage(
                file="handler/user.go",
                line=6,
                column=6,
                kind="context_method",
                symbol=f"{GIN}.(*Context).Query",
            )
        ],
        loading_errors=[],
    )
    inventory = build_migration_inventory(
        analysis,
        load_rule_registry(DEFAULT_RULE_PATH),
    )
    return RepositoryInspection.from_inventory(
        str(repository), [], inventory
    )


def _completed(repository: Path, bundle_count: int) -> FileMigrationRunResult:
    return FileMigrationRunResult(
        repository_path=str(repository.resolve()),
        status=FileMigrationRunStatus.COMPLETED,
        bundle_count=bundle_count,
        completed_files=["handler/user.go"] if bundle_count else [],
        records=[],
    )


def _completed_with_proposal(repository: Path, plan) -> FileMigrationRunResult:
    bundle = plan.bundles[0]
    proposal = FilePatchProposal(
        bundle_id=bundle.bundle_id,
        file=bundle.file,
        status=FilePatchStatus.NO_CHANGE_NEEDED,
        summary="already migrated",
        updated_content=None,
        confidence=PatchConfidence.HIGH,
        assumptions=[],
        manual_review_notes=[],
        unresolved_dependencies=[],
    )
    execution = FileBundleExecutionResult(
        bundle_id=bundle.bundle_id,
        file=bundle.file,
        usage_count=len(bundle.usage_ids),
        migration_rule_count=len(bundle.rule_ids),
        read_only_symbol_count=len(bundle.read_only_context),
        prompt_size=100,
        model_used="fake",
        original_sha256="0" * 64,
        proposal=proposal,
    )
    return FileMigrationRunResult(
        repository_path=str(repository.resolve()),
        status=FileMigrationRunStatus.COMPLETED,
        bundle_count=len(plan.bundles),
        completed_files=[bundle.file],
        records=[
            FileBundleRunRecord(
                bundle_id=bundle.bundle_id,
                file=bundle.file,
                status=FileBundleRunStatus.NO_CHANGE,
                execution=execution,
            )
        ],
    )


def _validation() -> FileMigrationValidationResult:
    return FileMigrationValidationResult(
        status=FileMigrationWorkflowStatus.COMPLETED,
        formatted_files=[],
        dependency_files_changed=[],
        command_results=[],
    )


def test_connects_existing_stages_and_writes_artifacts(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    output = tmp_path / "artifacts"
    inspection = _inspection(repository)
    model = Mock(model_name="fake")

    with (
        patch(
            "agent.file_migration_workflow.inspect_repository",
            return_value=inspection,
        ) as inspect,
        patch("agent.file_migration_workflow.run_file_migration") as run,
        patch(
            "agent.file_migration_workflow.validate_file_migration",
            return_value=_validation(),
        ) as validate,
    ):
        run.side_effect = lambda _repo, _inspection, plan, *_args, **_kwargs: (
            _completed_with_proposal(repository, plan)
        )
        result = migrate_files(
            repository,
            output,
            model,
            analyzer_timeout=90,
            apply_timeout=45,
        )

    assert result.bundle_plan.file_count == 1
    assert result.bundle_plan.bundles[0].file == "handler/user.go"
    assert inspect.call_args.kwargs["analyzer_timeout"] == 90
    assert run.call_args.kwargs["timeout_seconds"] == 45
    validate.assert_called_once()
    assert sorted(path.name for path in output.iterdir()) == [
        "file-bundle-plan.json",
        "file-classification.json",
        "file-migration-plan.json",
        "file-migration-result.json",
        "file-migration-workflow-result.json",
        "file-validation-result.json",
        "inspection.json",
        "proposals",
    ]
    proposal_files = list((output / "proposals").glob("*.json"))
    assert [path.stem for path in proposal_files] == [
        result.bundle_plan.bundles[0].bundle_id
    ]
    assert '"status": "no_change_needed"' in proposal_files[0].read_text()


def test_rejects_artifact_directory_inside_repository(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()

    with pytest.raises(ValueError, match="outside the migrated repository"):
        migrate_files(
            repository,
            repository / "output",
            Mock(model_name="fake"),
        )


def test_stopped_migration_skips_repository_validation(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    output = tmp_path / "artifacts"
    inspection = _inspection(repository)
    stopped = FileMigrationRunResult(
        repository_path=str(repository.resolve()),
        status=FileMigrationRunStatus.STOPPED,
        bundle_count=1,
        completed_files=[],
        records=[],
        failed_bundle_id="bundle-1",
    )

    with (
        patch(
            "agent.file_migration_workflow.inspect_repository",
            return_value=inspection,
        ),
        patch(
            "agent.file_migration_workflow.run_file_migration",
            return_value=stopped,
        ),
        patch(
            "agent.file_migration_workflow.validate_file_migration"
        ) as validate,
    ):
        result = migrate_files(repository, output, Mock(model_name="fake"))

    assert result.status is FileMigrationWorkflowStatus.MIGRATION_STOPPED
    assert result.validation is None
    validate.assert_not_called()
    assert not (output / "file-validation-result.json").exists()
    assert (output / "file-migration-workflow-result.json").exists()


def test_cli_runs_file_workflow_and_prints_summary(tmp_path: Path) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    output = tmp_path / "artifacts"
    inspection = _inspection(repository)
    classification = classify_gin_files(inspection)
    file_plan = build_file_migration_plan(classification)
    bundle_plan = build_file_bundle_plan(
        classification,
        file_plan,
        inspection,
        load_rule_registry(DEFAULT_RULE_PATH),
    )
    workflow = FileMigrationWorkflowResult(
        inspection=inspection,
        classification=classification,
        file_plan=file_plan,
        bundle_plan=bundle_plan,
        execution=_completed(repository, 1),
        status=FileMigrationWorkflowStatus.COMPLETED,
        validation=_validation(),
        output_directory=output,
    )
    stdout = StringIO()
    stderr = StringIO()

    with (
        patch("agent.migration_cli.create_file_bundle_model_client"),
        patch("agent.migration_cli.migrate_files", return_value=workflow),
    ):
        exit_code = main(
            [
                "migrate",
                "files",
                "--repo",
                str(repository),
                "--output-dir",
                str(output),
            ],
            stdout=stdout,
            stderr=stderr,
        )

    assert exit_code == 0
    assert "Gin files: 1" in stdout.getvalue()
    assert "Migration status: completed" in stdout.getvalue()
    assert str(output.resolve()) in stdout.getvalue()
    assert stderr.getvalue() == ""
