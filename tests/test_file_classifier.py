from __future__ import annotations

from knowledge.registry import load_rule_registry
from models.analyzer import AnalyzerResult, GinUsage, SymbolReference
from models.file_classification import FileRoleConfidence, GinFileRole
from models.inspection import RepositoryInspection
from tools.file_classifier import classify_gin_files
from tools.inspection import DEFAULT_RULE_PATH
from tools.migration_planner import build_migration_inventory

MODULE = "example.com/app"
GIN = "github.com/gin-gonic/gin"


def usage(file: str, line: int, kind: str, symbol: str) -> GinUsage:
    return GinUsage(file=file, line=line, column=2, kind=kind, symbol=symbol)


def reference(
    file: str,
    line: int,
    enclosing: str,
    symbol: str,
    *,
    package: str,
    definition_file: str = "",
    framework_bound: bool = False,
) -> SymbolReference:
    return SymbolReference(
        file=file,
        line=line,
        column=2,
        enclosing_symbol=enclosing,
        symbol=symbol,
        package_path=package,
        module_path=MODULE if package.startswith(MODULE) else package,
        definition_file=definition_file,
        definition_line=3 if definition_file else 0,
        signature="func()",
        framework_bound=framework_bound,
        resolved=True,
    )


def inspection(
    usages: list[GinUsage], references: list[SymbolReference]
) -> RepositoryInspection:
    analysis = AnalyzerResult(
        module_path=MODULE,
        packages=[],
        usages=usages,
        references=references,
        loading_errors=[],
    )
    inventory = build_migration_inventory(
        analysis,
        load_rule_registry(DEFAULT_RULE_PATH),
    )
    return RepositoryInspection.from_inventory(
        ".", [], inventory, references
    )


def by_file(result):
    return {item.file: item for item in result.files}


def test_classifies_bootstrap_router_handler_and_middleware_semantically() -> None:
    usages = [
        usage("main.go", 8, "constructor", f"{GIN}.Default"),
        usage("router.go", 12, "engine_method", f"{GIN}.(*Engine).GET"),
        usage("router.go", 13, "middleware", f"{GIN}.(*Engine).Use"),
        usage("handler/user.go", 5, "gin_type", f"{GIN}.Context"),
        usage("middleware/auth.go", 5, "gin_type", f"{GIN}.HandlerFunc"),
    ]
    references = [
        reference("main.go", 8, f"{MODULE}.main", f"{GIN}.Default", package=GIN),
        reference(
            "router.go",
            12,
            f"{MODULE}.Routes",
            f"{MODULE}/handler.GetUser",
            package=f"{MODULE}/handler",
            definition_file="handler/user.go",
            framework_bound=True,
        ),
        reference(
            "router.go",
            13,
            f"{MODULE}.Routes",
            f"{MODULE}/middleware.Auth",
            package=f"{MODULE}/middleware",
            definition_file="middleware/auth.go",
            framework_bound=True,
        ),
    ]

    files = by_file(classify_gin_files(inspection(usages, references)))

    assert files["main.go"].role is GinFileRole.BOOTSTRAP
    assert files["router.go"].role is GinFileRole.ROUTING
    assert files["handler/user.go"].role is GinFileRole.HANDLER
    assert files["middleware/auth.go"].role is GinFileRole.MIDDLEWARE
    assert all(item.confidence is FileRoleConfidence.EXACT for item in files.values())


def test_classifies_framework_bound_helper_referenced_by_handler_for_review() -> None:
    usages = [
        usage("router.go", 10, "engine_method", f"{GIN}.(*Engine).GET"),
        usage("handler/user.go", 5, "gin_type", f"{GIN}.Context"),
        usage("pkg/web/response.go", 7, "context_method", f"{GIN}.(*Context).JSON"),
    ]
    references = [
        reference(
            "router.go",
            10,
            f"{MODULE}.Routes",
            f"{MODULE}/handler.GetUser",
            package=f"{MODULE}/handler",
            definition_file="handler/user.go",
            framework_bound=True,
        ),
        reference(
            "handler/user.go",
            8,
            f"{MODULE}/handler.GetUser",
            f"{MODULE}/pkg/web.Success",
            package=f"{MODULE}/pkg/web",
            definition_file="pkg/web/response.go",
            framework_bound=True,
        ),
    ]

    files = by_file(classify_gin_files(inspection(usages, references)))
    helper = files["pkg/web/response.go"]

    assert helper.role is GinFileRole.SHARED_CONTEXT
    assert helper.confidence is FileRoleConfidence.REVIEW
    assert "referenced by a registered handler" in " ".join(helper.reasons)


def test_context_only_file_is_handler_review_not_forced_exact() -> None:
    result = classify_gin_files(
        inspection(
            [usage("feature.go", 6, "context_method", f"{GIN}.(*Context).Query")],
            [],
        )
    )

    assert result.files[0].role is GinFileRole.HANDLER
    assert result.files[0].confidence is FileRoleConfidence.REVIEW


def test_other_gin_file_defaults_to_shared_context_for_review() -> None:
    result = classify_gin_files(
        inspection(
            [usage("odd.go", 3, "gin_import", GIN)],
            [],
        )
    )

    assert result.files[0].role is GinFileRole.SHARED_CONTEXT
    assert result.files[0].confidence is FileRoleConfidence.REVIEW
    assert "project framework wrapper" in " ".join(result.files[0].reasons)


def test_conflicting_semantic_roles_require_review() -> None:
    usages = [
        usage("mixed.go", 8, "engine_method", f"{GIN}.(*Engine).GET"),
        usage("mixed.go", 9, "gin_type", f"{GIN}.HandlerFunc"),
    ]

    result = classify_gin_files(inspection(usages, []))
    classified = result.files[0]

    assert classified.role is GinFileRole.ROUTING
    assert classified.confidence is FileRoleConfidence.REVIEW
    assert classified.candidate_roles == [
        GinFileRole.ROUTING,
        GinFileRole.MIDDLEWARE,
    ]


def test_classification_is_deterministic_and_usage_ids_are_preserved() -> None:
    source = inspection(
        [
            usage("handler.go", 9, "context_method", f"{GIN}.(*Context).JSON"),
            usage("handler.go", 5, "gin_type", f"{GIN}.Context"),
        ],
        [],
    )

    first = classify_gin_files(source)
    second = classify_gin_files(source)

    assert first == second
    assert first.files[0].usage_ids == sorted(
        usage.usage_id for usage in source.discovered_gin_usages
    )
