"""CLI for one-command repository migration orchestration."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TextIO
from uuid import uuid4

from agent.repository_migration import migrate_repository
from agent.file_migration_workflow import migrate_files
from knowledge.registry import load_rule_registry
from llm.factory import (
    create_bundle_model_client,
    create_file_bundle_model_client,
)
from models.file_migration_execution import FileMigrationRunStatus
from models.file_migration_validation import FileMigrationWorkflowStatus
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
    files = subcommands.add_parser(
        "files",
        help="inspect, plan, generate, and apply ordered one-file bundles",
    )
    files.add_argument("--repo", required=True, type=Path)
    files.add_argument("--output-dir", type=Path)
    files.add_argument("--rules", type=Path, default=DEFAULT_RULE_PATH)
    files.add_argument("--provider", choices=("gemini", "aihubmix"))
    files.add_argument("--model")
    files.add_argument(
        "--analyzer-timeout",
        type=_positive_timeout,
        default=120.0,
    )
    files.add_argument(
        "--model-timeout",
        type=_positive_timeout,
        default=300.0,
    )
    files.add_argument(
        "--apply-timeout",
        type=_positive_timeout,
        default=30.0,
    )
    files.add_argument(
        "--validation-timeout",
        type=_positive_timeout,
        default=300.0,
    )
    files.add_argument("--allow-dirty", action="store_true")


def run_migration_command(
    args: argparse.Namespace,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    if args.migration_command == "files":
        return _run_file_migration_command(args, stdout, stderr)
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


def _run_file_migration_command(
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
        model = create_file_bundle_model_client(
            provider=args.provider,
            model_name=args.model,
            timeout_seconds=args.model_timeout,
        )
        workflow = migrate_files(
            args.repo,
            output_directory,
            model,
            analyzer_timeout=args.analyzer_timeout,
            apply_timeout=args.apply_timeout,
            validation_timeout=args.validation_timeout,
            allow_dirty=args.allow_dirty,
            rule_path=args.rules,
        )
    except (OSError, ValueError, RuntimeError) as error:
        print(f"error: {type(error).__name__}: {error}", file=stderr)
        return 1

    execution = workflow.execution
    applied = sum(record.status == "applied" for record in execution.records)
    unchanged = sum(
        record.status == "no_change" for record in execution.records
    )
    blocked = sum(record.status == "blocked" for record in execution.records)
    failed = sum(
        record.status in {"failed", "rolled_back"}
        for record in execution.records
    )
    print(f"Repository: {execution.repository_path}", file=stdout)
    print(f"Gin files: {len(workflow.classification.files)}", file=stdout)
    print(f"File bundles: {execution.bundle_count}", file=stdout)
    print(f"Applied files: {applied}", file=stdout)
    print(f"No-change files: {unchanged}", file=stdout)
    print(f"Blocked files: {blocked}", file=stdout)
    print(f"Failed files: {failed}", file=stdout)
    print(f"Migration status: {workflow.status.value}", file=stdout)
    print(
        f"Failed bundle: {execution.failed_bundle_id or 'none'}",
        file=stdout,
    )
    print(f"Result directory: {workflow.output_directory}", file=stdout)
    if workflow.validation is None:
        print("Validation: skipped (migration did not complete)", file=stdout)
    else:
        validation = workflow.validation
        print(
            "Formatted files: " + (", ".join(validation.formatted_files) or "none"),
            file=stdout,
        )
        print(
            "Dependency files changed: "
            + (", ".join(validation.dependency_files_changed) or "none"),
            file=stdout,
        )
        for command in validation.command_results:
            outcome = "timed_out" if command.timed_out else str(command.exit_code)
            print(f"{command.operation}: {outcome}", file=stdout)
    return 0 if workflow.status is FileMigrationWorkflowStatus.COMPLETED else 1


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
