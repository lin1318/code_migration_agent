"""CLI adapter for deterministic dependency readiness resolution."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TextIO

from agent.dependency_resolver import (
    MAX_DEPENDENCY_DEPTH,
    resolve_task_dependencies,
    select_dependency_task,
)
from agent.execution.errors import TaskExecutionError
from agent.execution.service import _load_plan
from knowledge.registry import RuleRegistryError, load_rule_registry
from tools.inspection import DEFAULT_RULE_PATH


def add_dependency_commands(commands) -> None:
    dependency = commands.add_parser(
        "dependency",
        help="resolve migration task dependencies without calling an LLM",
    )
    subcommands = dependency.add_subparsers(
        dest="dependency_command",
        required=True,
    )
    resolve = subcommands.add_parser(
        "resolve",
        help="classify dependencies and evaluate one task's readiness",
    )
    resolve.add_argument("--repo", required=True, type=Path)
    resolve.add_argument("--plan", required=True, type=Path)
    resolve.add_argument("--phase", required=True)
    resolve.add_argument("--task-order", type=int)
    resolve.add_argument(
        "--max-depth",
        type=_positive_depth,
        default=MAX_DEPENDENCY_DEPTH,
    )
    resolve.add_argument("--show-graph", action="store_true")


def run_dependency_command(
    args: argparse.Namespace,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    try:
        repository = args.repo.expanduser()
        if not repository.exists() or not repository.is_dir():
            raise ValueError(
                f"repository path is not a directory: {repository}"
            )
        inspection = _load_plan(args.plan)
        task = select_dependency_task(
            inspection,
            phase=args.phase,
            task_order=args.task_order,
        )
        registry = load_rule_registry(DEFAULT_RULE_PATH)
        result = resolve_task_dependencies(
            inspection,
            task,
            registry,
            max_depth=args.max_depth,
        )
    except (TaskExecutionError, RuleRegistryError, OSError, ValueError) as error:
        print(f"error: {error}", file=stderr)
        return 1

    print(f"Task ID: {result.task_id}", file=stdout)
    print(f"Readiness: {result.status.value}", file=stdout)
    print(f"Local dependencies: {len(result.local_dependencies)}", file=stdout)
    print(
        f"External dependencies: {len(result.external_dependencies)}",
        file=stdout,
    )
    print(f"Framework builtins: {len(result.framework_builtins)}", file=stdout)
    print(
        f"Generated child tasks: {len(result.generated_child_tasks)}",
        file=stdout,
    )
    print(
        f"Blocking dependencies: {len(result.blocking_dependencies)}",
        file=stdout,
    )
    if args.show_graph:
        print("Dependency graph:", file=stdout)
        for edge in result.dependency_graph.edges:
            print(
                f"  {edge.source} -[{edge.edge_type}]-> {edge.target}",
                file=stdout,
            )
    print(result.model_dump_json(indent=2), file=stdout)
    return 0


def _positive_depth(value: str) -> int:
    depth = int(value)
    if depth < 1:
        raise argparse.ArgumentTypeError("max depth must be at least 1")
    return depth
