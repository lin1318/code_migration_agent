import argparse
import sys
from pathlib import Path
from typing import TextIO

from langgraph.checkpoint.memory import InMemorySaver
from pydantic import BaseModel

from agent.bundle_cli import add_bundle_commands, run_bundle_command
from agent.dependency_cli import add_dependency_commands, run_dependency_command
from agent.patch_cli import add_patch_commands, run_patch_command
from knowledge.registry import RuleRegistryError
from models.inspection import RepositoryInspection
from models.workflow import WorkflowState
from tools.go_analyzer import AnalyzerError
from tools.inspection import (
    InspectionError,
    inspect_repository,
)
from workflow.service import run_inspection_workflow


def main(
    argv: list[str] | None = None,
    *,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    parser = argparse.ArgumentParser(
        prog="code-migration-agent",
        description="code migration agent",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    inspect_parser = commands.add_parser(
        "inspect",
        help="inspect a Go repository and build a deterministic migration plan",
    )
    inspect_parser.add_argument("--repo", required=True, type=Path)
    inspect_parser.add_argument("--output", type=Path)
    inspect_parser.add_argument(
        "--analyzer-timeout",
        type=_positive_timeout,
        default=30.0,
        metavar="SECONDS",
    )
    workflow_parser = commands.add_parser(
        "workflow",
        help="run deterministic LangGraph workflows",
    )
    workflow_commands = workflow_parser.add_subparsers(
        dest="workflow_command",
        required=True,
    )
    workflow_inspect = workflow_commands.add_parser(
        "inspect",
        help="inspect and plan through the deterministic StateGraph",
    )
    workflow_inspect.add_argument("--repo", required=True, type=Path)
    workflow_inspect.add_argument("--output", type=Path)
    workflow_inspect.add_argument("--rules", type=Path)
    workflow_inspect.add_argument(
        "--analyzer-timeout",
        type=_positive_timeout,
        default=30.0,
        metavar="SECONDS",
    )
    workflow_inspect.add_argument(
        "--checkpoint-memory",
        action="store_true",
        help="enable an in-memory checkpointer for this process",
    )
    add_patch_commands(commands)
    add_dependency_commands(commands)
    add_bundle_commands(commands)
    args = parser.parse_args(argv)

    if args.command == "inspect":
        return _run_inspect(args, stdout, stderr)
    if args.command == "workflow" and args.workflow_command == "inspect":
        return _run_workflow_inspect(args, stdout, stderr)
    if args.command == "patch" and args.patch_command == "generate":
        return run_patch_command(args, stdout, stderr)
    if args.command == "dependency" and args.dependency_command == "resolve":
        return run_dependency_command(args, stdout, stderr)
    if args.command == "bundle":
        return run_bundle_command(args, stdout, stderr)
    parser.error(f"unknown command: {args.command}")
    return 2


def _run_inspect(args: argparse.Namespace, stdout: TextIO, stderr: TextIO) -> int:
    try:
        inspection = inspect_repository(
            args.repo,
            analyzer_timeout=args.analyzer_timeout,
        )
        if args.output is not None:
            _write_output(args.output, args.repo, inspection)
    except (
        AnalyzerError,
        InspectionError,
        RuleRegistryError,
        OSError,
        ValueError,
    ) as error:
        print(f"error: {error}", file=stderr)
        return 1

    _print_summary(inspection, stdout)
    if args.output is not None:
        print(f"Migration plan JSON: {args.output.resolve()}", file=stdout)
    return 0


def _write_output(
    output: Path,
    repository: Path,
    inspection: RepositoryInspection,
) -> None:
    output = output.resolve()
    repository = repository.resolve()
    if output == repository or repository in output.parents:
        raise ValueError("output path must be outside the inspected repository")
    output.write_text(
        inspection.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )


def _print_summary(inspection: RepositoryInspection, stream: TextIO) -> None:
    summary = inspection.repository_summary
    packages = ", ".join(inspection.packages_containing_gin_usages) or "none"
    module = summary.module_path or "unavailable"
    lines = (
        ("Repository path", inspection.repository_path),
        ("Module path", module),
        ("Package count", summary.package_count),
        ("Go file count", summary.source_file_count),
        ("Packages containing Gin usages", packages),
        ("Total discovered Gin usages", len(inspection.discovered_gin_usages)),
        ("Exact migration count", len(inspection.exact_migrations)),
        (
            "Review-required migration count",
            len(inspection.migrations_requiring_review),
        ),
        ("Unsupported usage count", len(inspection.unsupported_usages)),
        ("Unmatched usage count", len(inspection.unmatched_usages)),
        ("Ordered migration task count", len(inspection.ordered_migration_tasks)),
    )
    for label, value in lines:
        print(f"{label}: {value}", file=stream)


def _positive_timeout(value: str) -> float:
    timeout = float(value)
    if timeout <= 0:
        raise argparse.ArgumentTypeError("timeout must be greater than zero")
    return timeout


def _run_workflow_inspect(
    args: argparse.Namespace,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    checkpointer = InMemorySaver() if args.checkpoint_memory else None
    if args.rules is None:
        state = run_inspection_workflow(
            args.repo,
            analyzer_timeout=args.analyzer_timeout,
            checkpointer=checkpointer,
        )
    else:
        state = run_inspection_workflow(
            args.repo,
            analyzer_timeout=args.analyzer_timeout,
            rule_registry_path=args.rules,
            checkpointer=checkpointer,
        )
    report = state.get("final_report")
    final_status = state.get("final_status")
    if report is None or final_status is None:
        print("error: workflow returned incomplete final state", file=stderr)
        return 1
    if args.output is not None:
        try:
            _write_workflow_output(args.output, args.repo, report)
        except (OSError, ValueError) as error:
            print(f"error: {error}", file=stderr)
            return 1
    _print_workflow_summary(state, stdout)
    if args.output is not None:
        print(f"Workflow result JSON: {args.output.resolve()}", file=stdout)
    if final_status == "failed":
        for error in state.get("errors", []):
            print(
                f"error [{error.stage}] {error.error_type}: {error.message}",
                file=stderr,
            )
        return 1
    return 0


def _write_workflow_output(
    output: Path,
    repository: Path,
    report: BaseModel,
) -> None:
    output = output.resolve()
    repository = repository.resolve()
    if output == repository or repository in output.parents:
        raise ValueError("output path must be outside the inspected repository")
    output.write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")


def _print_workflow_summary(state: WorkflowState, stream: TextIO) -> None:
    completed = ", ".join(state.get("completed_stages", [])) or "none"
    summary = state.get("repository_summary")
    inventory = state.get("migration_inventory")
    plan = state.get("migration_plan") or []
    print(f"Completed stages: {completed}", file=stream)
    print(
        f"Repository path: {state.get('repository_path', 'unavailable')}",
        file=stream,
    )
    if summary is not None and inventory is not None:
        print(f"Module path: {summary.module_path or 'unavailable'}", file=stream)
        print(f"Package count: {summary.package_count}", file=stream)
        print(f"Go file count: {summary.source_file_count}", file=stream)
        print(f"Total Gin usages: {summary.gin_usage_count}", file=stream)
        print(f"Gin import count: {summary.gin_import_count}", file=stream)
        print(
            f"Semantic Gin API usage count: {summary.gin_api_usage_count}",
            file=stream,
        )
        print(f"Exact migration count: {len(inventory.exact_migrations)}", file=stream)
        print(
            "Review-required count: "
            f"{len(inventory.migrations_requiring_review)}",
            file=stream,
        )
        print(f"Unsupported count: {len(inventory.unsupported_usages)}", file=stream)
        print(f"Unmatched count: {len(inventory.unmatched_usages)}", file=stream)
    print(f"Migration task count: {len(plan)}", file=stream)
    print(f"Warning count: {len(state.get('warnings', []))}", file=stream)
    print(
        f"Final status: {state.get('final_status', 'unavailable')}",
        file=stream,
    )
