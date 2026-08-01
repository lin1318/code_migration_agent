"""CLI for repository-wide deterministic migration bundle planning."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import TextIO

from knowledge.registry import RuleRegistryError, load_rule_registry
from agent.bundle_execution import execute_bundle, select_ready_bundle
from agent.bundle_apply import BundleApplyError, apply_bundle_patch
from agent.bundle_validation import validate_applied_bundle
from agent.execution.errors import TaskExecutionError
from llm.factory import create_bundle_model_client
from models.bundle_apply import (
    BundleApplyResult,
    BundleApplyStatus,
    PatchApplyRequest,
)
from models.bundle_execution import BundlePatchProposal
from models.bundles import MigrationBundle
from models.go_validation import (
    BundleValidationStatus,
    ValidationBaseline,
    ValidationLevel,
)
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
    apply = subcommands.add_parser(
        "apply",
        help="safely apply exactly one validated bundle proposal",
    )
    apply.add_argument("--repo", required=True, type=Path)
    apply.add_argument("--bundle", required=True, type=Path)
    apply.add_argument("--proposal", required=True, type=Path)
    apply.add_argument("--dry-run", action="store_true")
    apply.add_argument("--allow-dirty", action="store_true")
    apply.add_argument("--timeout", type=_positive_timeout, default=30.0)
    apply.add_argument("--output", type=Path)
    validate = subcommands.add_parser(
        "validate",
        help="format and validate one applied migration bundle",
    )
    validate.add_argument("--repo", required=True, type=Path)
    validate.add_argument("--bundle", required=True, type=Path)
    validate.add_argument("--proposal", required=True, type=Path)
    validate.add_argument("--apply-result", required=True, type=Path)
    validate.add_argument("--baseline", type=Path)
    validate.add_argument(
        "--level",
        choices=[item.value for item in ValidationLevel],
        default=ValidationLevel.FULL.value,
    )
    validate.add_argument("--fail-fast", action="store_true")
    validate.add_argument("--use-goimports", action="store_true")
    validate.add_argument("--run-mod-tidy", action="store_true")
    validate.add_argument("--timeout", type=_positive_timeout)
    validate.add_argument("--output", type=Path)


def run_bundle_command(
    args: argparse.Namespace,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    if args.bundle_command == "execute":
        return _run_bundle_execute(args, stdout, stderr)
    if args.bundle_command == "apply":
        return _run_bundle_apply(args, stdout, stderr)
    if args.bundle_command == "validate":
        return _run_bundle_validate(args, stdout, stderr)
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


def _run_bundle_apply(
    args: argparse.Namespace,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    try:
        bundle = MigrationBundle.model_validate_json(
            args.bundle.read_text(encoding="utf-8")
        )
        proposal = BundlePatchProposal.model_validate_json(
            args.proposal.read_text(encoding="utf-8")
        )
        request = PatchApplyRequest(
            repository_path=str(args.repo),
            bundle=bundle,
            proposal=proposal,
            allow_dirty=args.allow_dirty,
            dry_run=args.dry_run,
            timeout_seconds=args.timeout,
        )
        result = apply_bundle_patch(request)
        destination = None
        if args.output is not None:
            destination = _validated_output(args.output, args.repo)
            destination.write_text(
                result.model_dump_json(indent=2) + "\n",
                encoding="utf-8",
            )
    except (BundleApplyError, OSError, ValueError) as error:
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
    print(f"Apply status: {result.status.value}", file=stdout)
    print(
        "Changed files: " + (", ".join(result.changed_files) or "none"),
        file=stdout,
    )
    print(f"Rollback performed: {result.rollback_performed}", file=stdout)
    if destination is not None:
        print(f"Apply result JSON: {destination}", file=stdout)
    if result.git_diff:
        print("Git diff:", file=stdout)
        print(result.git_diff.rstrip(), file=stdout)
    return 1 if result.status in {
        BundleApplyStatus.ROLLED_BACK,
        BundleApplyStatus.FAILED,
    } else 0


def _run_bundle_validate(
    args: argparse.Namespace,
    stdout: TextIO,
    stderr: TextIO,
) -> int:
    try:
        bundle = MigrationBundle.model_validate_json(
            args.bundle.read_text(encoding="utf-8")
        )
        proposal = BundlePatchProposal.model_validate_json(
            args.proposal.read_text(encoding="utf-8")
        )
        apply_result = BundleApplyResult.model_validate_json(
            args.apply_result.read_text(encoding="utf-8")
        )
        baseline = (
            ValidationBaseline.model_validate_json(
                args.baseline.read_text(encoding="utf-8")
            )
            if args.baseline is not None
            else None
        )
        result = validate_applied_bundle(
            args.repo,
            bundle,
            proposal,
            apply_result,
            baseline=baseline,
            level=ValidationLevel(args.level),
            fail_fast=args.fail_fast,
            use_goimports=args.use_goimports,
            run_mod_tidy=args.run_mod_tidy,
            timeout_seconds=args.timeout,
        )
        destination = None
        if args.output is not None:
            destination = _validated_output(args.output, args.repo)
            destination.write_text(
                result.model_dump_json(indent=2) + "\n",
                encoding="utf-8",
            )
    except (OSError, ValueError) as error:
        print(f"error: {error}", file=stderr)
        return 1
    print(f"Bundle: {result.bundle_id}", file=stdout)
    print(
        "Modified files: " + (", ".join(proposal.modified_files) or "none"),
        file=stdout,
    )
    print(
        "Affected packages: " + (", ".join(result.affected_packages) or "none"),
        file=stdout,
    )
    for command in result.command_results:
        print(f"{command.operation}: {command.status.value}", file=stdout)
    print(f"Introduced failures: {len(result.introduced_diagnostics)}", file=stdout)
    print(f"Persistent failures: {len(result.persistent_diagnostics)}", file=stdout)
    print(f"Resolved failures: {len(result.resolved_diagnostics)}", file=stdout)
    print(f"Validation status: {result.status.value}", file=stdout)
    print(f"Repair required: {result.repair_required}", file=stdout)
    if destination is not None:
        print(f"Validation result JSON: {destination}", file=stdout)
    return 0 if result.status in {
        BundleValidationStatus.PASSED,
        BundleValidationStatus.PASSED_WITH_PREEXISTING_FAILURES,
    } else 1


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


def _positive_timeout(value: str) -> float:
    timeout = float(value)
    if timeout <= 0:
        raise argparse.ArgumentTypeError("timeout must be greater than zero")
    return timeout
