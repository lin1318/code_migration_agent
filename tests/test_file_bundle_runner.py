from __future__ import annotations

import difflib
import subprocess
from pathlib import Path
from unittest.mock import patch

from agent.file_bundle_runner import run_file_migration
from knowledge.registry import load_rule_registry
from models.analyzer import AnalyzerResult, GinUsage
from models.bundle_apply import BundleApplyResult, BundleApplyStatus
from models.file_bundle_execution import FilePatchProposal, FilePatchStatus
from models.file_bundles import FileBundlePlan, FileMigrationBundle
from models.file_migration_execution import (
    FileBundleRunStatus,
    FileMigrationRunStatus,
)
from models.file_migration_plan import FileMigrationStage
from models.inspection import RepositoryInspection
from models.patch_proposal import PatchConfidence
from tools.inspection import DEFAULT_RULE_PATH
from tools.migration_planner import build_migration_inventory

GIN = "github.com/gin-gonic/gin"


class ContextAwareClient:
    model_name = "fake-file-sequence"

    def __init__(self, *, blocked_file: str | None = None) -> None:
        self.blocked_file = blocked_file
        self.calls = []

    def generate_file_bundle_patch(self, context, prompt):
        del prompt
        self.calls.append(context)
        bundle = context.bundle
        if bundle.file == self.blocked_file:
            return FilePatchProposal(
                bundle_id=bundle.bundle_id,
                file=bundle.file,
                status=FilePatchStatus.BLOCKED,
                summary="manual migration required",
                patch=None,
                confidence=PatchConfidence.LOW,
                assumptions=[],
                manual_review_notes=["custom handler"],
                unresolved_dependencies=[],
            )
        source = context.editable_file.content.replace("\r\n", "\n")
        target = source.replace(
            'import "github.com/gin-gonic/gin"',
            'import hertzApp "github.com/cloudwego/hertz/pkg/app"',
        ).replace("*gin.Context", "*hertzApp.RequestContext")
        return FilePatchProposal(
            bundle_id=bundle.bundle_id,
            file=bundle.file,
            status=FilePatchStatus.PATCH_GENERATED,
            summary="migrate file",
            patch=_whole_file_patch(bundle.file, source, target),
            confidence=PatchConfidence.HIGH,
            assumptions=[],
            manual_review_notes=[],
            unresolved_dependencies=[],
        )


def _whole_file_patch(file: str, source: str, target: str) -> str:
    unified = "".join(
        difflib.unified_diff(
            source.splitlines(keepends=True),
            target.splitlines(keepends=True),
            fromfile=f"a/{file}",
            tofile=f"b/{file}",
            n=3,
        )
    )
    return f"diff --git a/{file} b/{file}\n{unified}"


def _inputs(repository: Path, files: list[str]):
    usages = []
    bundles = []
    for order, file in enumerate(files, start=1):
        target = repository / file
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            'package handler\n\nimport "github.com/gin-gonic/gin"\n\n'
            f'func Handler{order}(c *gin.Context) {{\n'
            '\t_ = c.Query("id")\n}\n',
            encoding="utf-8",
        )
        usages.append(
            GinUsage(
                file=file,
                line=6,
                column=6,
                kind="context_method",
                symbol=f"{GIN}.(*Context).Query",
            )
        )
    analysis = AnalyzerResult(
        module_path="example.com/app",
        packages=[],
        usages=usages,
        loading_errors=[],
    )
    registry = load_rule_registry(DEFAULT_RULE_PATH)
    inventory = build_migration_inventory(analysis, registry)
    inspection = RepositoryInspection.from_inventory(
        str(repository), [], inventory
    )
    usage_by_file = {
        usage.file: usage for usage in inspection.discovered_gin_usages
    }
    for order, file in enumerate(files, start=1):
        usage = usage_by_file[file]
        bundles.append(
            FileMigrationBundle(
                bundle_id=f"file-bundle-{order}",
                order=order,
                stage=FileMigrationStage.HANDLERS,
                file=file,
                usage_ids=[usage.usage_id],
                rule_ids=["gin.context.query"],
                framework_symbols=[usage.symbol],
                read_only_context=[],
                review_required=False,
                reasons=["handler migration"],
            )
        )
    plan = FileBundlePlan(
        file_count=len(files),
        usage_count=len(files),
        review_bundle_count=0,
        bundles=bundles,
        bundle_ordering=[bundle.bundle_id for bundle in bundles],
    )
    return inspection, registry, plan


def _applied(bundle_id: str, file: str) -> BundleApplyResult:
    return BundleApplyResult(
        bundle_id=bundle_id,
        status=BundleApplyStatus.APPLIED,
        applied_files=[],
        changed_files=[file],
        git_diff="diff",
        rollback_performed=False,
        rollback_succeeded=None,
        command_results=[],
        errors=[],
    )


def _git(repository: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        shell=False,
    )


def test_runs_files_in_order_and_passes_completed_files(tmp_path: Path) -> None:
    inspection, registry, plan = _inputs(tmp_path, ["a.go", "b.go"])
    client = ContextAwareClient()
    applications = [
        _applied(bundle.bundle_id, bundle.file) for bundle in plan.bundles
    ]

    with patch(
        "agent.file_bundle_runner.apply_bundle_patch",
        side_effect=applications,
    ) as apply:
        result = run_file_migration(
            tmp_path, inspection, plan, registry, client
        )

    assert result.status is FileMigrationRunStatus.COMPLETED
    assert result.completed_files == ["a.go", "b.go"]
    assert [context.completed_files for context in client.calls] == [
        [],
        ["a.go"],
    ]
    assert apply.call_count == 2


def test_stops_at_blocked_file_without_running_later_files(
    tmp_path: Path,
) -> None:
    inspection, registry, plan = _inputs(
        tmp_path, ["a.go", "b.go", "c.go"]
    )
    client = ContextAwareClient(blocked_file="b.go")

    with patch(
        "agent.file_bundle_runner.apply_bundle_patch",
        return_value=_applied("file-bundle-1", "a.go"),
    ) as apply:
        result = run_file_migration(
            tmp_path, inspection, plan, registry, client
        )

    assert result.status is FileMigrationRunStatus.STOPPED
    assert result.failed_bundle_id == "file-bundle-2"
    assert result.completed_files == ["a.go"]
    assert [record.status for record in result.records] == [
        FileBundleRunStatus.APPLIED,
        FileBundleRunStatus.BLOCKED,
    ]
    assert [context.bundle.file for context in client.calls] == ["a.go", "b.go"]
    assert apply.call_count == 1


def test_applies_each_file_directly_to_existing_repository(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "tests@example.com")
    _git(repository, "config", "user.name", "Tests")
    inspection, registry, plan = _inputs(repository, ["handler.go"])
    _git(repository, "add", "handler.go")
    _git(repository, "commit", "-m", "fixture")

    result = run_file_migration(
        repository, inspection, plan, registry, ContextAwareClient()
    )

    assert result.status is FileMigrationRunStatus.COMPLETED
    assert result.records[0].status is FileBundleRunStatus.APPLIED
    migrated = (repository / "handler.go").read_text(encoding="utf-8")
    assert "hertzApp.RequestContext" in migrated
    assert "gin.Context" not in migrated
    assert not any(path.is_dir() for path in repository.glob("migration-*"))
