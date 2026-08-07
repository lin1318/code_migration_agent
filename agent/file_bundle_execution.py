"""Build, invoke, and validate one single-file migration proposal."""

from __future__ import annotations

import json
from pathlib import Path
from string import Template
from typing import Protocol

from agent.execution.context_builder import _read_source_file, _repository_root
from agent.execution.errors import (
    ContextBuildError,
    ProposalValidationError,
    SuspiciousNoChangeProposalError,
    TaskSelectionError,
)
from agent.execution.validator import (
    normalize_patch_hunk_counts,
    validate_patch_document,
    validate_patch_old_content,
)
from knowledge.registry import RuleRegistry
from models.file_bundle_execution import (
    FileBundleExecutionContext,
    FileBundleExecutionResult,
    FilePatchProposal,
    FilePatchStatus,
)
from models.file_bundles import FileBundlePlan, FileMigrationBundle
from models.inspection import RepositoryInspection
from models.migration_rules import MigrationRule

PROMPT_PATH = (
    Path(__file__).resolve().parents[1] / "prompts" / "file_bundle_execution.md"
)


class FileBundleModelClient(Protocol):
    @property
    def model_name(self) -> str: ...

    def generate_file_bundle_patch(
        self,
        context: FileBundleExecutionContext,
        prompt: str,
    ) -> FilePatchProposal: ...


def select_file_bundle(
    plan: FileBundlePlan,
    bundle_id: str,
    *,
    completed_files: list[str] | None = None,
) -> FileMigrationBundle:
    matches = [bundle for bundle in plan.bundles if bundle.bundle_id == bundle_id]
    if len(matches) != 1:
        raise TaskSelectionError(
            "select_file_bundle",
            f"expected exactly one bundle {bundle_id!r}, found {len(matches)}",
        )
    bundle = matches[0]
    completed = {_normalized(path) for path in completed_files or []}
    if _normalized(bundle.file) in completed:
        raise TaskSelectionError(
            "select_file_bundle",
            f"file bundle has already been completed: {bundle.file}",
        )
    expected_index = plan.bundle_ordering.index(bundle.bundle_id)
    pending_previous = [
        earlier.file
        for earlier in plan.bundles[:expected_index]
        if _normalized(earlier.file) not in completed
    ]
    if pending_previous:
        raise TaskSelectionError(
            "select_file_bundle",
            f"file bundle is not next in order; pending files: {pending_previous}",
        )
    return bundle


def build_file_bundle_context(
    repository: str | Path,
    inspection: RepositoryInspection,
    bundle: FileMigrationBundle,
    registry: RuleRegistry,
    *,
    completed_files: list[str] | None = None,
) -> FileBundleExecutionContext:
    root = _repository_root(repository)
    source = _read_source_file(root, bundle.file)
    usages_by_id = {
        usage.usage_id: usage for usage in inspection.discovered_gin_usages
    }
    missing_usages = sorted(set(bundle.usage_ids) - set(usages_by_id))
    if missing_usages:
        raise ContextBuildError(
            "build_file_bundle_context",
            f"bundle references unknown usage IDs: {missing_usages}",
        )
    usages = [usages_by_id[usage_id] for usage_id in bundle.usage_ids]
    if any(_normalized(usage.file) != _normalized(bundle.file) for usage in usages):
        raise ContextBuildError(
            "build_file_bundle_context",
            "bundle usages must belong to its editable file",
        )

    rules_by_id = {rule.rule_id: rule for rule in registry.rules}
    missing_rules = sorted(set(bundle.rule_ids) - set(rules_by_id))
    if missing_rules:
        raise ContextBuildError(
            "build_file_bundle_context",
            f"bundle references unknown migration rules: {missing_rules}",
        )
    return FileBundleExecutionContext(
        repository_summary=inspection.repository_summary,
        bundle=bundle,
        editable_file={"path": bundle.file, "content": source},
        usages=usages,
        migration_rules=[rules_by_id[rule_id] for rule_id in bundle.rule_ids],
        read_only_context=bundle.read_only_context,
        completed_files=sorted(
            {path.replace("\\", "/") for path in completed_files or []},
            key=str.casefold,
        ),
    )


def build_file_bundle_prompt(context: FileBundleExecutionContext) -> str:
    try:
        template = PROMPT_PATH.read_text(encoding="utf-8")
        return Template(template).substitute(
            editable_file_path=context.editable_file.path,
            context_json=context.model_dump_json(indent=2)
        )
    except (OSError, KeyError, ValueError) as error:
        raise ContextBuildError(
            "build_file_bundle_prompt",
            f"cannot build file bundle prompt: {error}",
        ) from error


def validate_file_patch_proposal(
    proposal: FilePatchProposal,
    context: FileBundleExecutionContext,
) -> FilePatchProposal:
    bundle = context.bundle
    if proposal.bundle_id != bundle.bundle_id:
        raise ProposalValidationError(
            "validate_file_proposal",
            f"proposal bundle_id {proposal.bundle_id!r} does not match "
            f"{bundle.bundle_id!r}",
        )
    if _normalized(proposal.file) != _normalized(bundle.file):
        raise ProposalValidationError(
            "validate_file_proposal",
            f"proposal file {proposal.file!r} does not match {bundle.file!r}",
        )
    if proposal.status is FilePatchStatus.NO_CHANGE_NEEDED:
        _validate_no_change_needed(context)
        return proposal
    if proposal.patch is None:
        return proposal

    validate_patch_document(
        proposal.patch,
        expected_file=bundle.file,
        allowed_files={bundle.file},
    )
    validate_patch_old_content(
        proposal.patch,
        source=context.editable_file.content,
        expected_file=bundle.file,
    )
    return proposal


def execute_file_bundle(
    repository: str | Path,
    inspection: RepositoryInspection,
    bundle: FileMigrationBundle,
    registry: RuleRegistry,
    model_client: FileBundleModelClient,
    *,
    completed_files: list[str] | None = None,
) -> FileBundleExecutionResult:
    context = build_file_bundle_context(
        repository,
        inspection,
        bundle,
        registry,
        completed_files=completed_files,
    )
    prompt = build_file_bundle_prompt(context)
    proposal: FilePatchProposal | None = None
    for attempt in range(2):
        proposal = model_client.generate_file_bundle_patch(context, prompt)
        if proposal.patch is not None:
            proposal = proposal.model_copy(
                update={"patch": normalize_patch_hunk_counts(proposal.patch)}
            )
        try:
            validate_file_patch_proposal(proposal, context)
            break
        except ProposalValidationError as error:
            if error.details.operation != "validate_patch_source" or attempt == 1:
                raise
            prompt = _source_correction_prompt(
                prompt, error, context.editable_file.content
            )
    assert proposal is not None
    return FileBundleExecutionResult(
        bundle_id=bundle.bundle_id,
        file=bundle.file,
        usage_count=len(context.usages),
        migration_rule_count=len(context.migration_rules),
        read_only_symbol_count=len(context.read_only_context),
        prompt_size=len(prompt),
        model_used=model_client.model_name,
        proposal=proposal,
    )


def _validate_no_change_needed(context: FileBundleExecutionContext) -> None:
    changing_rules = [
        rule
        for rule in context.migration_rules
        if _rule_requires_source_change(rule)
    ]
    matched_usage_ids = {
        usage.usage_id
        for usage in context.usages
        if any(
            usage.symbol == rule.resolved_source_symbol
            for rule in changing_rules
        )
    }
    unresolved_usage_ids = {
        usage.usage_id
        for usage in context.usages
        if usage.kind != "gin_import"
        and not any(
            usage.symbol == rule.resolved_source_symbol
            for rule in context.migration_rules
        )
    }
    source_has_gin_import = "github.com/gin-gonic/gin" in (
        context.editable_file.content
    )
    if matched_usage_ids or unresolved_usage_ids or source_has_gin_import:
        raise SuspiciousNoChangeProposalError(
            "validate_no_change_needed",
            "no_change_needed conflicts with owned Gin usages or current source",
            diagnostics={
                "file": context.bundle.file,
                "rule_ids": ",".join(rule.rule_id for rule in changing_rules),
                "usage_ids": ",".join(
                    sorted(matched_usage_ids | unresolved_usage_ids)
                ),
                "gin_import_present": source_has_gin_import,
            },
        )


def _rule_requires_source_change(rule: MigrationRule) -> bool:
    if rule.target_operations:
        return True
    return (
        rule.source_package != rule.target_package
        or rule.source_symbol != rule.target_symbol
    )


def _source_correction_prompt(
    prompt: str,
    error: ProposalValidationError,
    source: str,
) -> str:
    return (
        prompt
        + "\n\nCORRECTION REQUIRED:\n"
        + error.details.message
        + "\nGenerate a new proposal from this exact current editable source. "
        + "Do not reuse the previous diff:\n"
        + json.dumps(source, ensure_ascii=False)
    )


def _normalized(path: str) -> str:
    return path.replace("\\", "/").casefold()
