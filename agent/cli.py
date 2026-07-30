import argparse
import sys
from pathlib import Path
from typing import TextIO

from knowledge.registry import RuleRegistryError
from models.inspection import RepositoryInspection
from tools.go_analyzer import AnalyzerError
from tools.inspection import (
    InspectionError,
    inspect_repository,
)


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
    args = parser.parse_args(argv)

    if args.command == "inspect":
        return _run_inspect(args, stdout, stderr)
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
