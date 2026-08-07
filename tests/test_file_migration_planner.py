from __future__ import annotations

import pytest

from models.file_classification import (
    ClassifiedGinFile,
    FileRoleConfidence,
    GinFileClassification,
    GinFileRole,
)
from models.file_migration_plan import FileMigrationStage
from tools.file_migration_planner import (
    build_file_migration_plan,
    validate_file_migration_plan,
)


def classified(
    file: str,
    role: GinFileRole,
    *usage_ids: str,
    confidence: FileRoleConfidence = FileRoleConfidence.EXACT,
) -> ClassifiedGinFile:
    return ClassifiedGinFile(
        file=file,
        role=role,
        confidence=confidence,
        usage_ids=list(usage_ids),
        symbols=[f"symbol.{usage_id}" for usage_id in usage_ids],
        candidate_roles=[role],
        reasons=["test evidence"],
    )


def test_builds_fixed_order_file_migration_stages() -> None:
    classification = GinFileClassification(
        files=[
            classified("main.go", GinFileRole.BOOTSTRAP, "usage-main"),
            classified("router.go", GinFileRole.ROUTING, "usage-route"),
            classified("middleware/auth.go", GinFileRole.MIDDLEWARE, "usage-auth"),
            classified("handler/user.go", GinFileRole.HANDLER, "usage-user"),
            classified(
                "pkg/web/response.go",
                GinFileRole.SHARED_CONTEXT,
                "usage-response",
            ),
        ]
    )

    plan = build_file_migration_plan(classification)

    assert [stage.stage for stage in plan.stages] == list(FileMigrationStage)
    assert [stage.order for stage in plan.stages] == [1, 2, 3, 4, 5]
    assert [stage.files for stage in plan.stages] == [
        ["pkg/web/response.go"],
        ["handler/user.go"],
        ["middleware/auth.go"],
        ["router.go"],
        ["main.go"],
    ]


def test_all_usages_in_a_file_share_one_stage_owner() -> None:
    classification = GinFileClassification(
        files=[
            classified(
                "handler/user.go",
                GinFileRole.HANDLER,
                "usage-context",
                "usage-param",
                "usage-json",
            )
        ]
    )

    plan = build_file_migration_plan(classification)
    handlers = next(
        stage for stage in plan.stages if stage.stage is FileMigrationStage.HANDLERS
    )

    assert handlers.files == ["handler/user.go"]
    assert handlers.usage_ids == ["usage-context", "usage-json", "usage-param"]
    assert all(
        not stage.usage_ids
        for stage in plan.stages
        if stage.stage is not FileMigrationStage.HANDLERS
    )


def test_review_classification_is_preserved_in_stage_plan() -> None:
    classification = GinFileClassification(
        files=[
            classified(
                "pkg/web/context.go",
                GinFileRole.SHARED_CONTEXT,
                "usage-context",
                confidence=FileRoleConfidence.REVIEW,
            )
        ]
    )

    plan = build_file_migration_plan(classification)
    stage = plan.stages[0]

    assert stage.review_required is True
    assert stage.review_files == ["pkg/web/context.go"]
    assert plan.review_file_count == 1


def test_rejects_duplicate_classified_file_ownership() -> None:
    classification = GinFileClassification(
        files=[
            classified("handler/user.go", GinFileRole.HANDLER, "usage-one"),
            classified("handler\\user.go", GinFileRole.HANDLER, "usage-two"),
        ]
    )

    with pytest.raises(ValueError, match="classified files are not unique"):
        build_file_migration_plan(classification)


def test_rejects_usage_owned_by_multiple_files() -> None:
    classification = GinFileClassification(
        files=[
            classified("handler/one.go", GinFileRole.HANDLER, "usage-shared"),
            classified("handler/two.go", GinFileRole.HANDLER, "usage-shared"),
        ]
    )

    with pytest.raises(ValueError, match="usage IDs are not uniquely owned"):
        build_file_migration_plan(classification)


def test_validation_rejects_missing_file() -> None:
    classification = GinFileClassification(
        files=[classified("handler/user.go", GinFileRole.HANDLER, "usage-user")]
    )
    plan = build_file_migration_plan(classification)
    handlers = plan.stages[1].model_copy(update={"files": []})
    invalid = plan.model_copy(
        update={"stages": [plan.stages[0], handlers, *plan.stages[2:]]}
    )

    with pytest.raises(ValueError, match="does not exactly cover classified files"):
        validate_file_migration_plan(invalid, classification)


def test_plan_is_deterministic_for_reordered_input() -> None:
    first_file = classified("handler/a.go", GinFileRole.HANDLER, "usage-a")
    second_file = classified("handler/b.go", GinFileRole.HANDLER, "usage-b")

    first = build_file_migration_plan(
        GinFileClassification(files=[second_file, first_file])
    )
    second = build_file_migration_plan(
        GinFileClassification(files=[first_file, second_file])
    )

    assert first == second
