"""CLI for non-mutating Go validation baseline capture."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TextIO

from agent.bundle_validation import capture_validation_baseline


def add_validation_commands(commands) -> None:
    validate = commands.add_parser(
        "validate",
        help="capture deterministic Go validation baselines",
    )
    subcommands = validate.add_subparsers(
        dest="validation_command",
        required=True,
    )
    baseline = subcommands.add_parser(
        "baseline",
        help="capture non-mutating repository build, test, and vet results",
    )
    baseline.add_argument("--repo", required=True, type=Path)
    baseline.add_argument("--output", required=True, type=Path)
    baseline.add_argument("--timeout", type=_positive_timeout)


def run_validation_command(
    args: argparse.Namespace,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    try:
        result = capture_validation_baseline(
            args.repo,
            timeout_seconds=args.timeout,
        )
        destination = _output_path(args.output, args.repo)
        destination.write_text(
            result.model_dump_json(indent=2) + "\n",
            encoding="utf-8",
        )
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=stderr)
        return 1
    print(f"Repository revision: {result.repository_revision}", file=stdout)
    print(f"Validation commands: {len(result.command_results)}", file=stdout)
    print(f"Diagnostics: {len(result.diagnostics)}", file=stdout)
    print(f"Baseline JSON: {destination}", file=stdout)
    return 0


def _output_path(output: Path, repository: Path) -> Path:
    destination = output.expanduser().resolve()
    root = repository.expanduser().resolve()
    if destination == root or root in destination.parents:
        raise ValueError("baseline output must be outside the source repository")
    return destination


def _positive_timeout(value: str) -> float:
    timeout = float(value)
    if timeout <= 0:
        raise argparse.ArgumentTypeError("timeout must be greater than zero")
    return timeout
