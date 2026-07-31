"""Build, execute, and validate exactly one ready Migration Bundle."""

from __future__ import annotations

from pathlib import Path
from string import Template
from typing import Protocol

from agent.execution.context_builder import (
    CONTEXT_LINE_RADIUS,
    _read_source_file,
    _repository_root,
)
from agent.execution.errors import (
    ContextBuildError,
    ProposalValidationError,
    TaskSelectionError,
)
from agent.execution.models import (
    NumberedSourceLine,
    RelatedSymbolDefinition,
    RelevantLineRange,
    RelevantSourceFile,
)
from agent.execution.validator import validate_patch_document
from knowledge.registry import RuleRegistry
from models.bundle_execution import (
    BundleExecutionResult,
    BundlePatchProposal,
    BundlePatchStatus,
    MigrationBundleContext,
)
from models.bundles import MigrationBundle, RepositoryMigrationPlan
from models.inspection import RepositoryInspection

PROMPT_PATH = Path(__file__).resolve().parents[1] / "prompts" / "bundle_execution.md"


class BundleModelClient(Protocol):
    @property
    def model_name(self) -> str: ...

    def generate_bundle_patch(
        self,
        context: MigrationBundleContext,
        prompt: str,
    ) -> BundlePatchProposal: ...


def select_ready_bundle(
    plan: RepositoryMigrationPlan,
    bundle_id: str,
    *,
    completed_bundles: list[str] | None = None,
) -> MigrationBundle:
    completed = set(completed_bundles or [])
    matches = [bundle for bundle in plan.bundles if bundle.bundle_id == bundle_id]
    if len(matches) != 1:
        raise TaskSelectionError(
            "select_bundle",
            f"expected exactly one bundle {bundle_id!r}, found {len(matches)}",
        )
    bundle = matches[0]
    if bundle.blocking_dependencies:
        raise TaskSelectionError(
            "select_bundle",
            f"bundle {bundle_id} has blocking external dependencies",
        )
    pending_dependencies = sorted(set(bundle.depends_on) - completed)
    if pending_dependencies:
        raise TaskSelectionError(
            "select_bundle",
            f"bundle {bundle_id} is not ready; pending bundles: "
            f"{pending_dependencies}",
        )
    return bundle


def build_bundle_context(
    repository: str | Path,
    inspection: RepositoryInspection,
    plan: RepositoryMigrationPlan,
    bundle: MigrationBundle,
    registry: RuleRegistry,
    *,
    completed_bundles: list[str] | None = None,
) -> MigrationBundleContext:
    root = _repository_root(repository)
    allowed_files = sorted(set(bundle.files))
    contents = {path: _read_source_file(root, path) for path in allowed_files}
    relevant_usages = [
        usage
        for usage in inspection.discovered_gin_usages
        if usage.file in contents and usage.kind != "gin_import"
    ]
    ranges = _source_ranges(relevant_usages, contents)
    related: dict[tuple[str, str, int], RelatedSymbolDefinition] = {}
    for reference in inspection.semantic_references:
        if reference.file not in contents and reference.definition_file not in contents:
            continue
        if not reference.definition_file or not reference.definition_line:
            continue
        definition = RelatedSymbolDefinition(
            symbol=reference.symbol,
            path=reference.definition_file,
            line=reference.definition_line,
            signature=reference.signature or reference.symbol,
        )
        related[(definition.symbol, definition.path, definition.line)] = definition
    matching = {
        rule.rule_id: rule
        for symbol in bundle.framework_symbols
        if (rule := registry.lookup_resolved_symbol(symbol)) is not None
    }
    replacements = {
        rule_id: rule
        for dependency in bundle.external_dependencies
        for rule_id in dependency.matching_rule_ids
        if (rule := next(
            (item for item in registry.rules if item.rule_id == rule_id),
            None,
        ))
        is not None
    }
    completed = sorted(set(completed_bundles or []))
    pending = sorted(
        item.bundle_id
        for item in plan.bundles
        if item.bundle_id != bundle.bundle_id and item.bundle_id not in completed
    )
    definitions = sorted(
        related.values(), key=lambda item: (item.path, item.line, item.symbol)
    )
    return MigrationBundleContext(
        repository_summary=inspection.repository_summary,
        bundle=bundle,
        allowed_files=allowed_files,
        framework_symbols=sorted(set(bundle.framework_symbols)),
        migration_concerns=sorted(set(bundle.migration_concerns)),
        relevant_source_files=[
            RelevantSourceFile(path=path, content=contents[path])
            for path in allowed_files
        ],
        relevant_source_ranges=ranges,
        related_type_definitions=[
            item for item in definitions if not item.signature.startswith("func")
        ],
        related_function_signatures=[
            item for item in definitions if item.signature.startswith("func")
        ],
        matching_migration_rules=sorted(
            matching.values(), key=lambda rule: rule.rule_id
        ),
        known_replacement_rules=sorted(
            replacements.values(), key=lambda rule: rule.rule_id
        ),
        completed_bundles=completed,
        pending_bundles=pending,
    )


def build_bundle_prompt(context: MigrationBundleContext) -> str:
    try:
        template = PROMPT_PATH.read_text(encoding="utf-8")
        return Template(template).substitute(
            context_json=context.model_dump_json(indent=2)
        )
    except (OSError, KeyError, ValueError) as error:
        raise ContextBuildError(
            "build_bundle_prompt",
            f"cannot build bundle prompt: {error}",
        ) from error


def validate_bundle_proposal(
    proposal: BundlePatchProposal,
    bundle: MigrationBundle,
) -> BundlePatchProposal:
    if proposal.bundle_id != bundle.bundle_id:
        raise ProposalValidationError(
            "validate_bundle_proposal",
            f"proposal bundle_id {proposal.bundle_id!r} does not match "
            f"{bundle.bundle_id!r}",
        )
    declared = proposal.modified_files
    if len(declared) != len(set(declared)):
        raise ProposalValidationError(
            "validate_bundle_proposal",
            "modified_files contains duplicates",
        )
    patch_files = [item.file for item in proposal.patches]
    if len(patch_files) != len(set(patch_files)):
        raise ProposalValidationError(
            "validate_bundle_proposal",
            "duplicate modifications of the same file are not allowed",
        )
    if set(declared) != set(patch_files):
        raise ProposalValidationError(
            "validate_bundle_proposal",
            "patch file list must exactly match modified_files",
        )
    allowed = set(bundle.files)
    if not set(declared).issubset(allowed):
        raise ProposalValidationError(
            "validate_bundle_proposal",
            "proposal modifies files outside the bundle",
        )
    if proposal.status == BundlePatchStatus.PATCH_GENERATED:
        for item in proposal.patches:
            _validate_bundle_patch(item.file, item.patch, allowed)
    elif proposal.patches:
        for item in proposal.patches:
            _validate_bundle_patch(item.file, item.patch, allowed)
    return proposal


def _validate_bundle_patch(
    file: str,
    patch: str,
    allowed_files: set[str],
) -> None:
    try:
        validate_patch_document(
            patch,
            expected_file=file,
            allowed_files=allowed_files,
        )
    except ProposalValidationError as error:
        first_line = next(
            (line.strip() for line in patch.splitlines() if line.strip()),
            "<empty>",
        )
        if len(first_line) > 120:
            first_line = first_line[:117] + "..."
        raise ProposalValidationError(
            error.details.operation,
            f"invalid patch for {file!r}: {error.details.message}; "
            f"first non-empty line: {first_line!r}",
            diagnostics={"file": file, "first_line": first_line},
        ) from error


def execute_bundle(
    repository: str | Path,
    inspection: RepositoryInspection,
    plan: RepositoryMigrationPlan,
    bundle: MigrationBundle,
    registry: RuleRegistry,
    model_client: BundleModelClient,
    *,
    completed_bundles: list[str] | None = None,
) -> BundleExecutionResult:
    context = build_bundle_context(
        repository,
        inspection,
        plan,
        bundle,
        registry,
        completed_bundles=completed_bundles,
    )
    prompt = build_bundle_prompt(context)
    proposal = model_client.generate_bundle_patch(context, prompt)
    validate_bundle_proposal(proposal, bundle)
    return BundleExecutionResult(
        bundle_id=bundle.bundle_id,
        allowed_file_count=len(context.allowed_files),
        source_range_count=len(context.relevant_source_ranges),
        related_symbol_count=(
            len(context.related_type_definitions)
            + len(context.related_function_signatures)
        ),
        migration_rule_count=(
            len(context.matching_migration_rules)
            + len(context.known_replacement_rules)
        ),
        prompt_size=len(prompt),
        model_used=model_client.model_name,
        proposal=proposal,
    )


def _source_ranges(usages, contents: dict[str, str]) -> list[RelevantLineRange]:
    ranges: list[RelevantLineRange] = []
    seen: set[tuple[str, int, int]] = set()
    for usage in sorted(usages, key=lambda item: (item.file, item.line)):
        lines = contents[usage.file].splitlines()
        start = max(1, usage.line - CONTEXT_LINE_RADIUS)
        end = min(len(lines), usage.line + CONTEXT_LINE_RADIUS)
        key = (usage.file, start, end)
        if key in seen:
            continue
        seen.add(key)
        ranges.append(
            RelevantLineRange(
                path=usage.file,
                start_line=start,
                end_line=end,
                lines=[
                    NumberedSourceLine(number=line, text=lines[line - 1])
                    for line in range(start, end + 1)
                ],
            )
        )
    return ranges
