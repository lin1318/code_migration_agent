from __future__ import annotations

import pytest

from knowledge.registry import load_rule_registry
from models.analyzer import AnalyzerResult, GinUsage, SymbolReference
from models.file_bundles import FileBundlePlan
from models.file_classification import (
    ClassifiedGinFile,
    FileRoleConfidence,
    GinFileClassification,
    GinFileRole,
)
from models.inspection import RepositoryInspection
from tools.file_bundle_planner import (
    build_file_bundle_plan,
    validate_file_bundle_plan,
)
from tools.file_migration_planner import build_file_migration_plan
from tools.inspection import DEFAULT_RULE_PATH
from tools.migration_planner import build_migration_inventory

GIN = "github.com/gin-gonic/gin"


def classified(
    file: str,
    role: GinFileRole,
    *symbols: str,
    confidence: FileRoleConfidence = FileRoleConfidence.EXACT,
) -> ClassifiedGinFile:
    return ClassifiedGinFile(
        file=file,
        role=role,
        confidence=confidence,
        usage_ids=[],
        symbols=list(symbols),
        candidate_roles=[role],
        reasons=[f"classified as {role.value}"],
    )


def build_inputs(
    classified_files: list[ClassifiedGinFile],
    references: list[SymbolReference] | None = None,
) -> tuple[GinFileClassification, RepositoryInspection]:
    usages = [
        GinUsage(
            file=item.file,
            line=index + 2,
            column=2,
            kind="gin_import" if symbol == GIN else "context_method",
            symbol=symbol,
        )
        for item in classified_files
        for index, symbol in enumerate(item.symbols)
    ]
    analysis = AnalyzerResult(
        module_path="example.com/app",
        packages=[],
        usages=usages,
        references=references or [],
        loading_errors=[],
    )
    inventory = build_migration_inventory(
        analysis,
        load_rule_registry(DEFAULT_RULE_PATH),
    )
    inspection = RepositoryInspection.from_inventory(
        ".", [], inventory, references or []
    )
    usage_ids_by_file: dict[str, list[str]] = {}
    for usage in inspection.discovered_gin_usages:
        usage_ids_by_file.setdefault(usage.file, []).append(usage.usage_id)
    classification = GinFileClassification(
        files=[
            item.model_copy(
                update={"usage_ids": sorted(usage_ids_by_file[item.file])}
            )
            for item in classified_files
        ]
    )
    return classification, inspection


def build_plan(
    classified_files: list[ClassifiedGinFile],
    references: list[SymbolReference] | None = None,
):
    classification, inspection = build_inputs(classified_files, references)
    file_plan = build_file_migration_plan(classification)
    registry = load_rule_registry(DEFAULT_RULE_PATH)
    return (
        build_file_bundle_plan(
            classification, file_plan, inspection, registry
        ),
        classification,
        file_plan,
        inspection,
    )


def test_creates_exactly_one_bundle_per_file_in_stage_order() -> None:
    plan, _, _, _ = build_plan(
        [
            classified("main.go", GinFileRole.BOOTSTRAP, f"{GIN}.Default"),
            classified(
                "handler/user.go",
                GinFileRole.HANDLER,
                f"{GIN}.(*Context).JSON",
            ),
            classified(
                "handler/article.go",
                GinFileRole.HANDLER,
                f"{GIN}.(*Context).Query",
            ),
        ]
    )

    assert [bundle.file for bundle in plan.bundles] == [
        "handler/article.go",
        "handler/user.go",
        "main.go",
    ]
    assert len({bundle.file for bundle in plan.bundles}) == plan.file_count == 3
    assert plan.bundle_ordering == [bundle.bundle_id for bundle in plan.bundles]


def test_bundle_contains_every_usage_symbol_and_rule_for_its_file() -> None:
    plan, _, _, _ = build_plan(
        [
            classified(
                "handler/user.go",
                GinFileRole.HANDLER,
                f"{GIN}.(*Context).Query",
                f"{GIN}.(*Context).JSON",
            )
        ]
    )

    bundle = plan.bundles[0]
    assert len(bundle.usage_ids) == 2
    assert bundle.framework_symbols == [
        f"{GIN}.(*Context).JSON",
        f"{GIN}.(*Context).Query",
    ]
    assert bundle.rule_ids == ["gin.context.json", "gin.context.query"]


def test_bundle_contains_cross_file_symbol_as_read_only_context() -> None:
    referenced = SymbolReference(
        file="handler/user.go",
        line=8,
        column=2,
        enclosing_symbol="example.com/app/handler.GetUser",
        symbol="example.com/app/service.FindUser",
        package_path="example.com/app/service",
        module_path="example.com/app",
        definition_file="service/user.go",
        definition_line=12,
        signature="func(id string) (*model.User, error)",
        framework_bound=False,
        resolved=True,
    )
    plan, _, _, _ = build_plan(
        [
            classified(
                "handler/user.go",
                GinFileRole.HANDLER,
                f"{GIN}.(*Context).Query",
            )
        ],
        [referenced],
    )

    assert [item.model_dump() for item in plan.bundles[0].read_only_context] == [
        {
            "symbol": "example.com/app/service.FindUser",
            "file": "service/user.go",
            "line": 12,
            "signature": "func(id string) (*model.User, error)",
            "framework_bound": False,
        }
    ]


def test_direct_gin_request_chain_adds_form_file_rule_only() -> None:
    direct = SymbolReference(
        file="handler/upload.go",
        line=10,
        column=18,
        enclosing_symbol="example.com/app/handler.Upload",
        symbol="net/http.(*Request).FormFile",
        package_path="net/http",
        signature=(
            "func(key string) (mime/multipart.File, "
            "*mime/multipart.FileHeader, error)"
        ),
        standard_library=True,
        resolved=True,
        receiver_root_type="*github.com/gin-gonic/gin.Context",
        receiver_path=["Request"],
    )
    plan, _, _, _ = build_plan(
        [
            classified(
                "handler/upload.go",
                GinFileRole.HANDLER,
                f"{GIN}.Context",
            )
        ],
        [direct],
    )

    assert "net-http.request.form-file-to-hertz" in plan.bundles[0].rule_ids

    standalone = direct.model_copy(
        update={
            "receiver_root_type": "*net/http.Request",
            "receiver_path": [],
        }
    )
    standalone_plan, _, _, _ = build_plan(
        [
            classified(
                "handler/upload.go",
                GinFileRole.HANDLER,
                f"{GIN}.Context",
            )
        ],
        [standalone],
    )

    assert (
        "net-http.request.form-file-to-hertz"
        not in standalone_plan.bundles[0].rule_ids
    )


def test_exact_semantic_reference_adds_rule_without_receiver_constraint() -> None:
    swagger = SymbolReference(
        file="routers/router.go",
        line=20,
        column=28,
        enclosing_symbol="example.com/app/routers.InitRouter",
        symbol="github.com/swaggo/gin-swagger.WrapHandler",
        package_path="github.com/swaggo/gin-swagger",
        signature="func(*webdav.Handler) gin.HandlerFunc",
        resolved=True,
    )
    plan, _, _, _ = build_plan(
        [
            classified(
                "routers/router.go",
                GinFileRole.ROUTING,
                f"{GIN}.(*Engine).GET",
            )
        ],
        [swagger],
    )

    assert "gin-swagger.wrap-handler" in plan.bundles[0].rule_ids


def test_same_file_reference_is_not_read_only_context() -> None:
    referenced = SymbolReference(
        file="handler/user.go",
        line=8,
        column=2,
        enclosing_symbol="example.com/app/handler.GetUser",
        symbol="example.com/app/handler.helper",
        package_path="example.com/app/handler",
        module_path="example.com/app",
        definition_file="handler/user.go",
        definition_line=20,
        signature="func()",
        resolved=True,
    )
    plan, _, _, _ = build_plan(
        [
            classified(
                "handler/user.go",
                GinFileRole.HANDLER,
                f"{GIN}.(*Context).Query",
            )
        ],
        [referenced],
    )

    assert plan.bundles[0].read_only_context == []


def test_external_definition_is_not_local_read_only_context() -> None:
    referenced = SymbolReference(
        file="handler/user.go",
        line=8,
        column=2,
        enclosing_symbol="example.com/app/handler.GetUser",
        symbol="net/http.StatusText",
        package_path="net/http",
        definition_file="../go/src/net/http/status.go",
        definition_line=90,
        signature="func(code int) string",
        standard_library=True,
        resolved=True,
    )
    plan, _, _, _ = build_plan(
        [
            classified(
                "handler/user.go",
                GinFileRole.HANDLER,
                f"{GIN}.(*Context).Query",
            )
        ],
        [referenced],
    )

    assert plan.bundles[0].read_only_context == []


def test_review_usage_or_file_marks_bundle_for_review() -> None:
    plan, _, _, _ = build_plan(
        [
            classified(
                "pkg/web/response.go",
                GinFileRole.SHARED_CONTEXT,
                f"{GIN}.(*Context).Unknown",
                confidence=FileRoleConfidence.REVIEW,
            )
        ]
    )

    bundle = plan.bundles[0]
    assert bundle.review_required is True
    assert plan.review_bundle_count == 1
    assert "review-required" in " ".join(bundle.reasons)


def test_bundle_ids_and_output_are_deterministic() -> None:
    files = [
        classified(
            "handler/b.go", GinFileRole.HANDLER, f"{GIN}.(*Context).JSON"
        ),
        classified(
            "handler/a.go", GinFileRole.HANDLER, f"{GIN}.(*Context).Query"
        ),
    ]

    first, _, _, _ = build_plan(files)
    second, _, _, _ = build_plan(list(reversed(files)))

    assert first == second


def test_validation_rejects_duplicate_file_bundle() -> None:
    plan, classification, file_plan, inspection = build_plan(
        [
            classified(
                "handler/user.go",
                GinFileRole.HANDLER,
                f"{GIN}.(*Context).JSON",
            )
        ]
    )
    duplicate = plan.bundles[0].model_copy(
        update={"bundle_id": "file-bundle-duplicate", "order": 2}
    )
    invalid = plan.model_copy(
        update={
            "file_count": 2,
            "bundles": [plan.bundles[0], duplicate],
            "bundle_ordering": [plan.bundles[0].bundle_id, duplicate.bundle_id],
        }
    )

    with pytest.raises(ValueError, match="multiple migration bundles"):
        validate_file_bundle_plan(
            invalid,
            classification,
            file_plan,
            inspection,
            load_rule_registry(DEFAULT_RULE_PATH),
        )


def test_validation_rejects_missing_file_usage() -> None:
    plan, classification, file_plan, inspection = build_plan(
        [
            classified(
                "handler/user.go",
                GinFileRole.HANDLER,
                f"{GIN}.(*Context).Query",
                f"{GIN}.(*Context).JSON",
            )
        ]
    )
    changed = plan.bundles[0].model_copy(
        update={"usage_ids": plan.bundles[0].usage_ids[:1]}
    )
    invalid = FileBundlePlan(
        file_count=1,
        usage_count=1,
        review_bundle_count=plan.review_bundle_count,
        bundles=[changed],
        bundle_ordering=[changed.bundle_id],
    )

    with pytest.raises(ValueError, match="does not contain all file usages"):
        validate_file_bundle_plan(
            invalid,
            classification,
            file_plan,
            inspection,
            load_rule_registry(DEFAULT_RULE_PATH),
        )
