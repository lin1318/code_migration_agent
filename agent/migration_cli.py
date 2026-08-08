"""CLI for one-command repository migration orchestration."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TextIO
from uuid import uuid4

from agent.file_migration_workflow import migrate_files
from llm.factory import create_file_bundle_model_client
from models.file_migration_validation import FileMigrationWorkflowStatus
from tools.inspection import DEFAULT_RULE_PATH


def add_migration_commands(commands) -> None:
    migrate = commands.add_parser(
        "migrate",
        help="run file-oriented repository migration",
    )
    subcommands = migrate.add_subparsers(dest="migration_command", required=True)
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
    return _run_file_migration_command(args, stdout, stderr)


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
