"""Deterministic stage planning with unique ownership of Gin-related files."""

from __future__ import annotations

from collections import Counter

from models.file_classification import (
    FileRoleConfidence,
    GinFileClassification,
    GinFileRole,
)
from models.file_migration_plan import (
    FileMigrationPlan,
    FileMigrationStage,
    FileMigrationStagePlan,
)

_STAGES = tuple(FileMigrationStage)
_ROLE_STAGE = {
    GinFileRole.SHARED_CONTEXT: FileMigrationStage.SHARED_CONTEXT,
    GinFileRole.HANDLER: FileMigrationStage.HANDLERS,
    GinFileRole.MIDDLEWARE: FileMigrationStage.MIDDLEWARE,
    GinFileRole.ROUTING: FileMigrationStage.ROUTING,
    GinFileRole.BOOTSTRAP: FileMigrationStage.BOOTSTRAP,
}


def build_file_migration_plan(
    classification: GinFileClassification,
) -> FileMigrationPlan:
    """Build the fixed-order file plan without executing or editing anything."""

    _validate_classification_identity(classification)
    by_stage = {stage: [] for stage in _STAGES}
    for classified in classification.files:
        by_stage[_ROLE_STAGE[classified.role]].append(classified)

    stages: list[FileMigrationStagePlan] = []
    for order, stage in enumerate(_STAGES, start=1):
        selected = sorted(
            by_stage[stage], key=lambda item: _normalized(item.file)
        )
        review_files = sorted(
            (
                item.file.replace("\\", "/")
                for item in selected
                if item.confidence is FileRoleConfidence.REVIEW
            ),
            key=str.casefold,
        )
        stages.append(
            FileMigrationStagePlan(
                order=order,
                stage=stage,
                files=[item.file.replace("\\", "/") for item in selected],
                usage_ids=sorted(
                    usage_id for item in selected for usage_id in item.usage_ids
                ),
                symbols=sorted(
                    {symbol for item in selected for symbol in item.symbols}
                ),
                review_required=bool(review_files),
                review_files=review_files,
            )
        )

    plan = FileMigrationPlan(
        file_count=len(classification.files),
        usage_count=sum(len(item.usage_ids) for item in classification.files),
        review_file_count=sum(
            item.confidence is FileRoleConfidence.REVIEW
            for item in classification.files
        ),
        stages=stages,
    )
    validate_file_migration_plan(plan, classification)
    return plan


def validate_file_migration_plan(
    plan: FileMigrationPlan,
    classification: GinFileClassification,
) -> None:
    """Validate stage order and exact, unique file and usage ownership."""

    _validate_classification_identity(classification)
    if [stage.stage for stage in plan.stages] != list(_STAGES):
        raise ValueError("file migration stages do not follow the required order")
    if [stage.order for stage in plan.stages] != list(range(1, 6)):
        raise ValueError("file migration stage order values must be 1 through 5")

    planned_files = [file for stage in plan.stages for file in stage.files]
    normalized_planned_files = [_normalized(file) for file in planned_files]
    duplicate_files = _duplicates(normalized_planned_files)
    if duplicate_files:
        raise ValueError(
            f"files have multiple migration stage owners: {duplicate_files}"
        )
    expected_files = {_normalized(item.file) for item in classification.files}
    if set(normalized_planned_files) != expected_files:
        raise ValueError("file migration plan does not exactly cover classified files")

    expected_stage = {
        _normalized(item.file): _ROLE_STAGE[item.role]
        for item in classification.files
    }
    for stage in plan.stages:
        for file in stage.files:
            if expected_stage[_normalized(file)] != stage.stage:
                raise ValueError(f"file {file!r} is assigned to the wrong stage")

        expected_items = [
            item
            for item in classification.files
            if _ROLE_STAGE[item.role] == stage.stage
        ]
        expected_stage_usage_ids = sorted(
            usage_id for item in expected_items for usage_id in item.usage_ids
        )
        if stage.usage_ids != expected_stage_usage_ids:
            raise ValueError(
                f"stage {stage.stage.value!r} does not own its files' usages"
            )
        expected_stage_symbols = sorted(
            {symbol for item in expected_items for symbol in item.symbols}
        )
        if stage.symbols != expected_stage_symbols:
            raise ValueError(
                f"stage {stage.stage.value!r} symbols do not match its files"
            )
        expected_stage_review_files = {
            _normalized(item.file)
            for item in expected_items
            if item.confidence is FileRoleConfidence.REVIEW
        }
        if {_normalized(file) for file in stage.review_files} != (
            expected_stage_review_files
        ):
            raise ValueError(
                f"stage {stage.stage.value!r} review files are incomplete"
            )

    planned_usage_ids = [
        usage_id for stage in plan.stages for usage_id in stage.usage_ids
    ]
    duplicate_usage_ids = _duplicates(planned_usage_ids)
    if duplicate_usage_ids:
        raise ValueError(
            "usages have multiple file migration stage owners: "
            f"{duplicate_usage_ids}"
        )
    expected_usage_ids = {
        usage_id for item in classification.files for usage_id in item.usage_ids
    }
    if set(planned_usage_ids) != expected_usage_ids:
        raise ValueError("file migration plan does not exactly cover classified usages")

    expected_review_files = {
        _normalized(item.file)
        for item in classification.files
        if item.confidence is FileRoleConfidence.REVIEW
    }
    planned_review_files = {
        _normalized(file)
        for stage in plan.stages
        for file in stage.review_files
    }
    if planned_review_files != expected_review_files:
        raise ValueError("file migration plan review files are incomplete")
    for stage in plan.stages:
        if stage.review_required != bool(stage.review_files):
            raise ValueError("stage review_required does not match review_files")

    if plan.file_count != len(expected_files):
        raise ValueError("file_count does not match classified files")
    if plan.usage_count != len(expected_usage_ids):
        raise ValueError("usage_count does not match classified usages")
    if plan.review_file_count != len(expected_review_files):
        raise ValueError("review_file_count does not match review files")


def _validate_classification_identity(
    classification: GinFileClassification,
) -> None:
    files = [_normalized(item.file) for item in classification.files]
    duplicate_files = _duplicates(files)
    if duplicate_files:
        raise ValueError(f"classified files are not unique: {duplicate_files}")
    usage_ids = [
        usage_id for item in classification.files for usage_id in item.usage_ids
    ]
    duplicate_usage_ids = _duplicates(usage_ids)
    if duplicate_usage_ids:
        raise ValueError(
            f"classified usage IDs are not uniquely owned: {duplicate_usage_ids}"
        )


def _duplicates(values: list[str]) -> list[str]:
    return sorted(value for value, count in Counter(values).items() if count > 1)


def _normalized(path: str) -> str:
    return path.replace("\\", "/").casefold()
