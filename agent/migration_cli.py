"""CLI for one-command repository migration orchestration."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TextIO
from uuid import uuid4

from agent.repository_migration import migrate_repository
from knowledge.registry import load_rule_registry
from llm.factory import create_bundle_model_client
from models.bundles import RepositoryMigrationPlan
from models.go_validation import ValidationBaseline
from models.repository_migration import RepositoryMigrationStatus
from tools.inspection import DEFAULT_RULE_PATH, inspect_repository


def add_migration_commands(commands) -> None:
    migrate = commands.add_parser(
        "migrate",
        help="orchestrate complete repository migration bundles",
    )
    subcommands = migrate.add_subparsers(dest="migration_command", required=True)
    repository = subcommands.add_parser(
        "repository",
        help="migrate all bundles then reconcile and validate the repository",
    )
    repository.add_argument("--repo", required=True, type=Path)
    repository.add_argument("--plan", required=True, type=Path)
    repository.add_argument(
        "--output-dir",
        type=Path,
        help=(
            "artifact directory (default: "
            "output/migration/migration-run-<generated-id>)"
        ),
    )
    repository.add_argument("--baseline", type=Path)
    repository.add_argument("--model")
    repository.add_argument(
        "--analyzer-timeout",
        type=_positive_timeout,
        default=120.0,
        help="Go analyzer timeout in seconds (default: 120)",
    )
    repository.add_argument("--timeout", type=_positive_timeout, default=300.0)
    repository.add_argument("--dry-run", action="store_true")


def run_migration_command(
    args: argparse.Namespace,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    output_directory = (
        args.output_dir.expanduser().resolve()
        if args.output_dir is not None
        else _default_output_directory()
    )
    try:
        plan = RepositoryMigrationPlan.model_validate_json(
            args.plan.read_text(encoding="utf-8")
        )
        baseline = (
            ValidationBaseline.model_validate_json(
                args.baseline.read_text(encoding="utf-8")
            )
            if args.baseline is not None
            else None
        )
        inspection = inspect_repository(
            args.repo,
            analyzer_timeout=args.analyzer_timeout,
        )
        registry = load_rule_registry(DEFAULT_RULE_PATH)
        model = (
            None
            if args.dry_run
            else create_bundle_model_client(model_name=args.model)
        )
        result = migrate_repository(
            args.repo,
            plan,
            inspection,
            registry,
            model,
            output_directory,
            baseline=baseline,
            dry_run=args.dry_run,
            timeout_seconds=args.timeout,
        )
        if not args.dry_run:
            output_directory.mkdir(parents=True, exist_ok=True)
            (output_directory / "repository-migration-result.json").write_text(
                result.model_dump_json(indent=2) + "\n",
                encoding="utf-8",
            )
    except (OSError, ValueError, RuntimeError) as error:
        print(f"error: {error}", file=stderr)
        return 1
    print(f"Repository: {result.repository_path}", file=stdout)
    print(f"Migration status: {result.status.value}", file=stdout)
    print(f"Bundle count: {len(result.bundle_order)}", file=stdout)
    print(f"Bundle records: {len(result.bundle_records)}", file=stdout)
    print(
        "Applied bundles: "
        f"{sum(record.status == 'applied' for record in result.bundle_records)}",
        file=stdout,
    )
    print(
        "Changed files: " + (", ".join(result.changed_files) or "none"),
        file=stdout,
    )
    print(
        "Dependency files changed: "
        + (", ".join(result.dependency_files_changed) or "none"),
        file=stdout,
    )
    print(f"Introduced failures: {len(result.introduced_diagnostics)}", file=stdout)
    print(f"Persistent failures: {len(result.persistent_diagnostics)}", file=stdout)
    print(f"Repair required: {result.repair_required}", file=stdout)
    for error in result.errors:
        print(f"Error: {error}", file=stderr)
    if not args.dry_run:
        print(
            "Migration result JSON: "
            f"{output_directory / 'repository-migration-result.json'}",
            file=stdout,
        )
    return 0 if result.status in {
        RepositoryMigrationStatus.PLANNED,
        RepositoryMigrationStatus.COMPLETED,
        RepositoryMigrationStatus.COMPLETED_WITH_PREEXISTING_FAILURES,
    } else 1


def _default_output_directory() -> Path:
    return (
        Path.cwd()
        / "output"
        / "migration"
        / f"migration-run-{uuid4().hex}"
    ).resolve()


def _positive_timeout(value: str) -> float:
    timeout = float(value)
    if timeout <= 0:
        raise argparse.ArgumentTypeError("timeout must be greater than zero")
    return timeout
