"""Build deterministic one-file migration bundles without executing them."""

from __future__ import annotations

import hashlib
from collections import Counter

from knowledge.registry import RuleRegistry
from models.file_bundles import (
    FileBundlePlan,
    FileMigrationBundle,
    ReadOnlySymbolContext,
)
from models.file_classification import FileRoleConfidence, GinFileClassification
from models.file_migration_plan import FileMigrationPlan, FileMigrationStage
from models.inspection import RepositoryInspection
from models.migration_rules import MigrationConfidence, MigrationRule
from tools.file_migration_planner import validate_file_migration_plan


def build_file_bundle_plan(
    classification: GinFileClassification,
    file_plan: FileMigrationPlan,
    inspection: RepositoryInspection,
    registry: RuleRegistry,
) -> FileBundlePlan:
    """Expand the uniquely owned file plan into one bundle per file."""

    validate_file_migration_plan(file_plan, classification)
    classified_by_file = {
        _normalized(item.file): item for item in classification.files
    }
    rules_by_usage_id, review_usage_ids = _usage_rule_metadata(inspection)
    semantic_rules_by_file = _semantic_rule_metadata(inspection, registry)

    bundles: list[FileMigrationBundle] = []
    for stage_plan in file_plan.stages:
        for file in stage_plan.files:
            classified = classified_by_file[_normalized(file)]
            usage_ids = sorted(classified.usage_ids)
            semantic_rules = semantic_rules_by_file.get(
                _normalized(file), []
            )
            rule_ids = sorted(
                {
                    rule_id
                    for usage_id in usage_ids
                    if (rule_id := rules_by_usage_id.get(usage_id)) is not None
                }
                | {rule.rule_id for rule in semantic_rules}
            )
            has_review_usage = any(
                usage_id in review_usage_ids for usage_id in usage_ids
            ) or any(
                rule.confidence is not MigrationConfidence.EXACT
                for rule in semantic_rules
            )
            reasons = list(classified.reasons)
            if has_review_usage:
                reasons.append(
                    "file contains review-required, unsupported, or unmatched usages"
                )
            bundles.append(
                FileMigrationBundle(
                    bundle_id=_bundle_id(stage_plan.stage, file),
                    order=len(bundles) + 1,
                    stage=stage_plan.stage,
                    file=file.replace("\\", "/"),
                    usage_ids=usage_ids,
                    rule_ids=rule_ids,
                    framework_symbols=sorted(set(classified.symbols)),
                    read_only_context=_read_only_context(file, inspection),
                    review_required=(
                        classified.confidence is FileRoleConfidence.REVIEW
                        or has_review_usage
                    ),
                    reasons=reasons,
                )
            )

    plan = FileBundlePlan(
        file_count=len(bundles),
        usage_count=sum(len(bundle.usage_ids) for bundle in bundles),
        review_bundle_count=sum(bundle.review_required for bundle in bundles),
        bundles=bundles,
        bundle_ordering=[bundle.bundle_id for bundle in bundles],
    )
    validate_file_bundle_plan(
        plan, classification, file_plan, inspection, registry
    )
    return plan


def validate_file_bundle_plan(
    plan: FileBundlePlan,
    classification: GinFileClassification,
    file_plan: FileMigrationPlan,
    inspection: RepositoryInspection,
    registry: RuleRegistry,
) -> None:
    """Validate one-to-one file bundles, contents, rules, and ordering."""

    validate_file_migration_plan(file_plan, classification)
    expected_files = {
        _normalized(item.file): item for item in classification.files
    }
    bundle_files = [_normalized(bundle.file) for bundle in plan.bundles]
    duplicate_files = _duplicates(bundle_files)
    if duplicate_files:
        raise ValueError(f"files have multiple migration bundles: {duplicate_files}")
    if set(bundle_files) != set(expected_files):
        raise ValueError("file bundle plan does not exactly cover migration files")

    bundle_ids = [bundle.bundle_id for bundle in plan.bundles]
    duplicate_bundle_ids = _duplicates(bundle_ids)
    if duplicate_bundle_ids:
        raise ValueError(f"file bundle IDs are not unique: {duplicate_bundle_ids}")
    if plan.bundle_ordering != bundle_ids:
        raise ValueError("bundle_ordering must match deterministic bundle order")
    if [bundle.order for bundle in plan.bundles] != list(
        range(1, len(plan.bundles) + 1)
    ):
        raise ValueError("file bundle order values must be consecutive")

    expected_order = [
        _normalized(file)
        for stage in file_plan.stages
        for file in stage.files
    ]
    if bundle_files != expected_order:
        raise ValueError("file bundles do not follow the Stage 2 file order")

    expected_stage_by_file = {
        _normalized(file): stage.stage
        for stage in file_plan.stages
        for file in stage.files
    }
    rules_by_usage_id, review_usage_ids = _usage_rule_metadata(inspection)
    semantic_rules_by_file = _semantic_rule_metadata(inspection, registry)
    all_bundle_usage_ids: list[str] = []
    for bundle in plan.bundles:
        classified = expected_files[_normalized(bundle.file)]
        semantic_rules = semantic_rules_by_file.get(
            _normalized(bundle.file), []
        )
        if bundle.stage != expected_stage_by_file[_normalized(bundle.file)]:
            raise ValueError(f"bundle for {bundle.file!r} has the wrong stage")
        if bundle.bundle_id != _bundle_id(bundle.stage, bundle.file):
            raise ValueError(f"bundle for {bundle.file!r} has an unstable ID")
        expected_usage_ids = sorted(classified.usage_ids)
        if bundle.usage_ids != expected_usage_ids:
            raise ValueError(
                f"bundle for {bundle.file!r} does not contain all file usages"
            )
        expected_rule_ids = sorted(
            {
                rule_id
                for usage_id in expected_usage_ids
                if (rule_id := rules_by_usage_id.get(usage_id)) is not None
            }
            | {rule.rule_id for rule in semantic_rules}
        )
        if bundle.rule_ids != expected_rule_ids:
            raise ValueError(
                f"bundle for {bundle.file!r} has incorrect migration rules"
            )
        if bundle.framework_symbols != sorted(set(classified.symbols)):
            raise ValueError(
                f"bundle for {bundle.file!r} has incorrect framework symbols"
            )
        if bundle.read_only_context != _read_only_context(
            bundle.file, inspection
        ):
            raise ValueError(
                f"bundle for {bundle.file!r} has incorrect read-only context"
            )
        expected_review = (
            classified.confidence is FileRoleConfidence.REVIEW
            or any(
                usage_id in review_usage_ids for usage_id in expected_usage_ids
            )
            or any(
                rule.confidence is not MigrationConfidence.EXACT
                for rule in semantic_rules
            )
        )
        if bundle.review_required != expected_review:
            raise ValueError(
                f"bundle for {bundle.file!r} has incorrect review status"
            )
        all_bundle_usage_ids.extend(bundle.usage_ids)

    duplicate_usage_ids = _duplicates(all_bundle_usage_ids)
    if duplicate_usage_ids:
        raise ValueError(
            f"usages have multiple migration bundles: {duplicate_usage_ids}"
        )
    expected_usage_ids = {
        usage_id for item in classification.files for usage_id in item.usage_ids
    }
    if set(all_bundle_usage_ids) != expected_usage_ids:
        raise ValueError("file bundles do not exactly cover classified usages")

    if plan.file_count != len(expected_files):
        raise ValueError("file_count does not match file bundles")
    if plan.usage_count != len(expected_usage_ids):
        raise ValueError("usage_count does not match bundle usages")
    if plan.review_bundle_count != sum(
        bundle.review_required for bundle in plan.bundles
    ):
        raise ValueError("review_bundle_count does not match review bundles")


def _usage_rule_metadata(
    inspection: RepositoryInspection,
) -> tuple[dict[str, str], set[str]]:
    rules: dict[str, str] = {}
    review: set[str] = set()
    for migration in inspection.exact_migrations:
        rules[migration.usage.usage_id] = migration.rule_id
    for migration in inspection.migrations_requiring_review:
        rules[migration.usage.usage_id] = migration.rule_id
        review.add(migration.usage.usage_id)
    for unsupported in inspection.unsupported_usages:
        if unsupported.rule_id is not None:
            rules[unsupported.usage.usage_id] = unsupported.rule_id
        review.add(unsupported.usage.usage_id)
    review.update(item.usage.usage_id for item in inspection.unmatched_usages)
    return rules, review


def _semantic_rule_metadata(
    inspection: RepositoryInspection,
    registry: RuleRegistry,
) -> dict[str, list[MigrationRule]]:
    rules_by_file: dict[str, dict[str, MigrationRule]] = {}
    for reference in inspection.semantic_references:
        rule = registry.lookup_resolved_symbol(reference.symbol)
        if rule is None:
            continue
        receiver = rule.source_receiver
        if receiver is not None and (
            reference.receiver_root_type != receiver.root_type
            or reference.receiver_path != receiver.access_path
        ):
            continue
        rules_by_file.setdefault(_normalized(reference.file), {})[
            rule.rule_id
        ] = rule
    return {
        file: [rules[rule_id] for rule_id in sorted(rules)]
        for file, rules in rules_by_file.items()
    }


def _read_only_context(
    file: str,
    inspection: RepositoryInspection,
) -> list[ReadOnlySymbolContext]:
    current = _normalized(file)
    module_path = inspection.repository_summary.module_path
    contexts: dict[tuple[str, str, int], ReadOnlySymbolContext] = {}
    for reference in inspection.semantic_references:
        definition_file = reference.definition_file
        if (
            _normalized(reference.file) != current
            or not definition_file
            or _normalized(definition_file) == current
            or not reference.resolved
            or not _is_local_package(reference.package_path, module_path)
        ):
            continue
        item = ReadOnlySymbolContext(
            symbol=reference.symbol,
            file=definition_file.replace("\\", "/"),
            line=reference.definition_line or None,
            signature=reference.signature or reference.symbol,
            framework_bound=reference.framework_bound,
        )
        contexts[(item.symbol, _normalized(item.file), item.line or 0)] = item
    return sorted(
        contexts.values(),
        key=lambda item: (_normalized(item.file), item.line or 0, item.symbol),
    )


def _bundle_id(stage: FileMigrationStage, file: str) -> str:
    identity = f"{stage.value}\0{_normalized(file)}".encode()
    return "file-bundle-" + hashlib.sha256(identity).hexdigest()[:16]


def _duplicates(values: list[str]) -> list[str]:
    return sorted(value for value, count in Counter(values).items() if count > 1)


def _normalized(path: str) -> str:
    return path.replace("\\", "/").casefold()


def _is_local_package(package_path: str, module_path: str) -> bool:
    return bool(module_path) and (
        package_path == module_path
        or package_path.startswith(f"{module_path}/")
    )
