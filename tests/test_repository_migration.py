from __future__ import annotations

import io
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from agent.cli import main
from agent.repository_migration import migrate_repository
from models.bundle_execution import BundleFilePatch, BundlePatchProposal
from models.bundles import (
    BundleComplexity,
    BundleDependencyGraph,
    MigrationBundle,
    RepositoryDependencyGraph,
    RepositoryMigrationJob,
    RepositoryMigrationPlan,
)
from models.repository_migration import RepositoryMigrationStatus


def _git(repository: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", *arguments],
        cwd=repository,
        capture_output=True,
        text=True,
        check=True,
        shell=False,
    ).stdout


def _repository(tmp_path: Path) -> Path:
    repository = tmp_path / "repository"
    repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "migration@example.com")
    _git(repository, "config", "user.name", "Migration Tests")
    (repository / "go.mod").write_text("module example.com/migrate\n\ngo 1.23\n")
    (repository / "main.go").write_text(
        'package migrate\n\nfunc Message() string { return "old" }\n'
    )
    _git(repository, "add", ".")
    _git(repository, "commit", "-m", "fixture")
    return repository


def _bundle(bundle_id: str = "bundle-one", file: str = "main.go") -> MigrationBundle:
    return MigrationBundle(
        bundle_id=bundle_id,
        files=[file],
        framework_symbols=[],
        migration_concerns=["handler_signatures"],
        external_dependencies=[],
        blocking_dependencies=[],
        depends_on=[],
        estimated_complexity=BundleComplexity.LOW,
    )


def _plan(repository: Path, bundles: list[MigrationBundle]) -> RepositoryMigrationPlan:
    return RepositoryMigrationPlan(
        repository_job=RepositoryMigrationJob(
            job_id="migration-job-test",
            repository_path=str(repository),
            module_path="example.com/migrate",
            framework="gin",
            target_framework="hertz",
        ),
        dependency_graph=RepositoryDependencyGraph(
            nodes=[], edges=[], cycles=[], unreachable_node_ids=[]
        ),
        bundles=bundles,
        bundle_graph=BundleDependencyGraph(
            nodes=[item.bundle_id for item in bundles], edges=[], cycles=[]
        ),
        ready_bundle_ordering=[item.bundle_id for item in bundles],
    )


def _proposal(bundle_id: str, file: str = "main.go") -> BundlePatchProposal:
    patch_text = (
        f"diff --git a/{file} b/{file}\n"
        f"--- a/{file}\n+++ b/{file}\n"
        "@@ -1,3 +1,3 @@\n"
        " package migrate\n \n"
        '-func Message() string { return "old" }\n'
        '+func Message() string { return "old" } // migrated\n'
    )
    return BundlePatchProposal(
        bundle_id=bundle_id,
        status="patch_generated",
        summary="Migrate repository fixture.",
        patches=[BundleFilePatch(file=file, patch=patch_text)],
        modified_files=[file],
        confidence="high",
        assumptions=[],
        manual_review_notes=[],
        unresolved_dependencies=[],
    )


def test_repository_migration_dry_run_does_not_call_model_or_mutate(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    plan = _plan(repository, [_bundle()])
    before = (repository / "main.go").read_bytes()

    result = migrate_repository(
        repository,
        plan,
        MagicMock(),
        MagicMock(),
        None,
        tmp_path / "artifacts",
        dry_run=True,
    )

    assert result.status == RepositoryMigrationStatus.PLANNED
    assert (repository / "main.go").read_bytes() == before
    assert not (tmp_path / "artifacts").exists()


def test_repository_migration_applies_all_then_tidy_and_validates(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    bundle = _bundle()
    plan = _plan(repository, [bundle])
    generated = SimpleNamespace(proposal=_proposal(bundle.bundle_id))

    with patch("agent.repository_migration.execute_bundle", return_value=generated):
        result = migrate_repository(
            repository,
            plan,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            tmp_path / "artifacts",
        )

    assert result.status == RepositoryMigrationStatus.COMPLETED
    assert result.changed_files == ["main.go"]
    assert [item.operation for item in result.command_results] == [
        "gofmt",
        "go_mod_tidy",
        "go_build_repository",
        "go_test_repository",
        "go_vet_repository",
    ]
    assert "// migrated" in (repository / "main.go").read_text()
    assert (tmp_path / "artifacts" / "repository-migration-result.json").is_file()


def test_later_bundle_failure_rolls_back_earlier_bundles(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    second_file = repository / "second.go"
    second_file.write_text("package migrate\n")
    _git(repository, "add", "second.go")
    _git(repository, "commit", "-m", "second file")
    first = _bundle()
    second = _bundle("bundle-two", "second.go")
    plan = _plan(repository, [first, second])
    before = (repository / "main.go").read_bytes()

    with patch(
        "agent.repository_migration.execute_bundle",
        side_effect=[SimpleNamespace(proposal=_proposal(first.bundle_id)), ValueError("model failed")],
    ):
        result = migrate_repository(
            repository,
            plan,
            MagicMock(),
            MagicMock(),
            MagicMock(),
            tmp_path / "artifacts",
        )

    assert result.status == RepositoryMigrationStatus.ROLLED_BACK
    assert result.rollback_succeeded is True
    assert (repository / "main.go").read_bytes() == before
    assert _git(repository, "status", "--porcelain") == ""


def test_migrate_cli_dry_run_does_not_create_model(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    plan = _plan(repository, [_bundle()])
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(plan.model_dump_json(), encoding="utf-8")

    with (
        patch("agent.migration_cli.inspect_repository", return_value=MagicMock()),
        patch("agent.migration_cli.load_rule_registry", return_value=MagicMock()),
        patch("agent.migration_cli.create_bundle_model_client") as create_model,
    ):
        exit_code = main(
            [
                "migrate",
                "repository",
                "--repo",
                str(repository),
                "--plan",
                str(plan_path),
                "--output-dir",
                str(tmp_path / "artifacts"),
                "--dry-run",
            ],
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )

    assert exit_code == 0
    create_model.assert_not_called()


def test_migrate_cli_passes_configurable_analyzer_timeout(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    plan = _plan(repository, [_bundle()])
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(plan.model_dump_json(), encoding="utf-8")

    with (
        patch("agent.migration_cli.inspect_repository", return_value=MagicMock()) as inspect,
        patch("agent.migration_cli.load_rule_registry", return_value=MagicMock()),
        patch("agent.migration_cli.create_bundle_model_client") as create_model,
    ):
        exit_code = main(
            [
                "migrate",
                "repository",
                "--repo",
                str(repository),
                "--plan",
                str(plan_path),
                "--output-dir",
                str(tmp_path / "artifacts"),
                "--analyzer-timeout",
                "600",
                "--dry-run",
            ],
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )

    assert exit_code == 0
    inspect.assert_called_once_with(repository, analyzer_timeout=600.0)
    create_model.assert_not_called()


def test_migrate_cli_defaults_output_under_output_migration(tmp_path: Path) -> None:
    repository = _repository(tmp_path)
    plan = _plan(repository, [_bundle()])
    plan_path = tmp_path / "plan.json"
    plan_path.write_text(plan.model_dump_json(), encoding="utf-8")
    default_output = tmp_path / "output" / "migration" / "migration-run-test"

    with (
        patch("agent.migration_cli.inspect_repository", return_value=MagicMock()),
        patch("agent.migration_cli.load_rule_registry", return_value=MagicMock()),
        patch("agent.migration_cli.create_bundle_model_client") as create_model,
        patch(
            "agent.migration_cli._default_output_directory",
            return_value=default_output,
        ),
        patch(
            "agent.migration_cli.migrate_repository",
            wraps=migrate_repository,
        ) as migrate,
    ):
        exit_code = main(
            [
                "migrate",
                "repository",
                "--repo",
                str(repository),
                "--plan",
                str(plan_path),
                "--dry-run",
            ],
            stdout=io.StringIO(),
            stderr=io.StringIO(),
        )

    assert exit_code == 0
    assert migrate.call_args.args[5] == default_output
    assert not default_output.exists()
    create_model.assert_not_called()
