"""CLI for repository-wide deterministic migration bundle planning."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TextIO

from knowledge.registry import RuleRegistryError, load_rule_registry
from agent.bundle_execution import execute_bundle, select_ready_bundle
from agent.execution.errors import TaskExecutionError
from llm.factory import create_bundle_model_client
from tools.bundle_planner import build_repository_migration_plan
from tools.go_analyzer import AnalyzerError
from tools.inspection import (
    DEFAULT_RULE_PATH,
    InspectionError,
    inspect_repository,
)


def add_bundle_commands(commands) -> None:
    bundle = commands.add_parser(
        "bundle",
        help="build a repository-wide migration dependency and bundle plan",
    )
    subcommands = bundle.add_subparsers(dest="bundle_command", required=True)
    plan = subcommands.add_parser(
        "plan",
        help="analyze a repository and write deterministic migration bundles",
    )
    plan.add_argument("--repo", required=True, type=Path)
    plan.add_argument("--output", required=True, type=Path)
    plan.add_argument("--show-graph", action="store_true")
    execute = subcommands.add_parser(
        "execute",
        help="generate a validated proposal for exactly one ready bundle",
    )
    execute.add_argument("--repo", required=True, type=Path)
    execute.add_argument("--bundle", required=True)
    execute.add_argument("--output", required=True, type=Path)
    execute.add_argument("--model")


def run_bundle_command(
    args: argparse.Namespace,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    if args.bundle_command == "execute":
        return _run_bundle_execute(args, stdout, stderr)
    try:
        inspection = inspect_repository(args.repo, analyzer_timeout=30.0)
        registry = load_rule_registry(DEFAULT_RULE_PATH)
        plan = build_repository_migration_plan(inspection, registry)
        destination = _validated_output(args.output, args.repo)
        destination.write_text(
            plan.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
    except (
        AnalyzerError,
        InspectionError,
        RuleRegistryError,
        OSError,
        ValueError,
    ) as error:
        print(f"error: {error}", file=stderr)
        return 1

    blocked = sum(bool(bundle.blocking_dependencies) for bundle in plan.bundles)
    print(f"Repository migration job: {plan.repository_job.job_id}", file=stdout)
    print(f"Migration bundles: {len(plan.bundles)}", file=stdout)
    print(f"Blocked bundles: {blocked}", file=stdout)
    print(
        "Ready bundle ordering: "
        + (", ".join(plan.ready_bundle_ordering) or "none"),
        file=stdout,
    )
    if args.show_graph:
        print("Bundle dependency graph:", file=stdout)
        for edge in plan.bundle_graph.edges:
            print(
                f"  {edge.source_bundle_id} -> {edge.target_bundle_id}",
                file=stdout,
            )
    print(f"Repository migration plan: {destination}", file=stdout)
    return 0


def _run_bundle_execute(
    args: argparse.Namespace,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    try:
        inspection = inspect_repository(args.repo, analyzer_timeout=30.0)
        registry = load_rule_registry(DEFAULT_RULE_PATH)
        plan = build_repository_migration_plan(inspection, registry)
        bundle = select_ready_bundle(plan, args.bundle)
        client = create_bundle_model_client(model_name=args.model)
        result = execute_bundle(
            args.repo,
            inspection,
            plan,
            bundle,
            registry,
            client,
        )
        destination = _validated_output(args.output, args.repo)
        destination.write_text(
            result.proposal.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
    except (
        AnalyzerError,
        InspectionError,
        RuleRegistryError,
        TaskExecutionError,
        OSError,
        ValueError,
    ) as error:
        if isinstance(error, TaskExecutionError):
            details = error.details
            print(
                f"error [{details.operation}] {details.error_type}: "
                f"{details.message}",
                file=stderr,
            )
        else:
            print(f"error: {error}", file=stderr)
        return 1

    print(f"Bundle: {result.bundle_id}", file=stdout)
    print(f"Allowed files: {result.allowed_file_count}", file=stdout)
    print(f"Relevant source ranges: {result.source_range_count}", file=stdout)
    print(f"Related symbols: {result.related_symbol_count}", file=stdout)
    print(f"Migration rules: {result.migration_rule_count}", file=stdout)
    print(f"Prompt size: {result.prompt_size}", file=stdout)
    print(f"Model: {result.model_used}", file=stdout)
    print(f"Proposal status: {result.proposal.status.value}", file=stdout)
    print(f"Validated patches: {len(result.proposal.patches)}", file=stdout)
    print(f"Bundle proposal: {destination}", file=stdout)
    return 0


def _validated_output(output: Path, repository: Path) -> Path:
    destination = output.expanduser().resolve()
    root = repository.expanduser().resolve()
    if destination == root or root in destination.parents:
        raise ValueError("bundle output must be outside the source repository")
    return destination
