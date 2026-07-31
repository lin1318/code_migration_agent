"""Thin CLI adapter for one Stage 8 patch proposal."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TextIO

from agent.dependency_resolver import (
    resolve_task_dependencies,
    select_dependency_task,
)
from agent.execution.context_builder import stable_task_id
from agent.execution.errors import TaskExecutionError
from agent.execution.service import _load_plan, execute_migration_task
from knowledge.registry import load_rule_registry
from llm.factory import create_model_client
from models.dependencies import TaskReadinessResult
from tools.inspection import DEFAULT_RULE_PATH


def add_patch_commands(commands) -> None:
    patch = commands.add_parser(
        "patch",
        help="generate a structured patch proposal without modifying files",
    )
    subcommands = patch.add_subparsers(dest="patch_command", required=True)
    generate = subcommands.add_parser(
        "generate",
        help="generate a proposal for exactly one migration task",
    )
    generate.add_argument("--repo", required=True, type=Path)
    generate.add_argument("--plan", required=True, type=Path)
    generate.add_argument("--phase", required=True)
    generate.add_argument("--task-order", type=int)
    generate.add_argument("--output", type=Path)
    generate.add_argument("--model")


def run_patch_command(
    args: argparse.Namespace,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    try:
        inspection = _load_plan(args.plan)
        registry = load_rule_registry(DEFAULT_RULE_PATH)
        task = select_dependency_task(
            inspection,
            phase=args.phase,
            task_order=args.task_order,
        )
        readiness = resolve_task_dependencies(
            inspection,
            task,
            registry,
        )
        if not readiness.ready:
            print(
                f"Task readiness: {readiness.status.value}",
                file=stdout,
            )
            _print_blocking_dependencies(readiness, stdout)
            _print_unwritten_output(args.output, stdout)
            return 1
        client = create_model_client(model_name=args.model)
        result = execute_migration_task(
            args.repo,
            args.plan,
            client,
            phase=args.phase,
            task_order=args.task_order,
        )
        if isinstance(result, TaskReadinessResult):
            print(
                f"Task readiness: {result.status.value}",
                file=stdout,
            )
            _print_blocking_dependencies(result, stdout)
            _print_unwritten_output(args.output, stdout)
            return 1
        if args.output is not None:
            _write_proposal(args.output, args.repo, result.proposal)
    except (TaskExecutionError, OSError, UnicodeError, ValueError) as error:
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

    print(
        f"Selected task: {stable_task_id(result.selected_task)} "
        f"(order {result.selected_task.order}, "
        f"phase {result.selected_task.phase.value})",
        file=stdout,
    )
    print(
        f"Context summary: {result.context_summary.model_dump(mode='json')}",
        file=stdout,
    )
    print(f"Prompt size: {result.prompt_size}", file=stdout)
    print(f"Model used: {result.model_used}", file=stdout)
    print(f"Proposal summary: {result.proposal.summary}", file=stdout)
    if args.output is not None:
        print(f"Proposal JSON: {args.output.resolve()}", file=stdout)
    else:
        print(result.proposal.model_dump_json(indent=2), file=stdout)
    return 0


def _print_blocking_dependencies(
    readiness: TaskReadinessResult,
    stream: TextIO,
) -> None:
    child_dependencies = [
        dependency
        for dependency in readiness.blocking_dependencies
        if dependency.resolution.value == "create_child_task"
    ]
    hard_blocks = [
        dependency
        for dependency in readiness.blocking_dependencies
        if dependency.resolution.value == "block"
    ]
    if child_dependencies:
        print(
            f"Pending local migration tasks: {len(child_dependencies)}",
            file=stream,
        )
        for dependency in child_dependencies[:3]:
            print(f"  - {dependency.symbol}", file=stream)
        remaining = len(child_dependencies) - 3
        if remaining > 0:
            print(f"  ... and {remaining} more", file=stream)
    print(f"Hard blocking dependencies: {len(hard_blocks)}", file=stream)
    for dependency in hard_blocks:
        rules = ", ".join(dependency.matching_rule_ids) or "none"
        print(
            f"  - {dependency.symbol} "
            f"[{dependency.kind.value}/{dependency.resolution.value}]",
            file=stream,
        )
        print(f"    reason: {dependency.notes}", file=stream)
        print(f"    matching rules: {rules}", file=stream)
    print(
        "Run `python -m agent dependency resolve ...` for the complete "
        "dependency JSON and graph.",
        file=stream,
    )


def _print_unwritten_output(output: Path | None, stream: TextIO) -> None:
    if output is None:
        print("Patch proposal was not generated.", file=stream)
        return
    destination = output.expanduser().resolve()
    print(f"Patch proposal was not written: {destination}", file=stream)
    if destination.exists():
        print(
            "An existing output file was left unchanged; it contains an older "
            "proposal.",
            file=stream,
        )


def _write_proposal(output: Path, repository: Path, proposal) -> None:
    destination = output.expanduser().resolve()
    repository_root = repository.expanduser().resolve()
    if destination == repository_root or repository_root in destination.parents:
        raise ValueError("proposal output must be outside the source repository")
    destination.write_text(
        proposal.model_dump_json(indent=2) + "\n",
        encoding="utf-8",
    )
