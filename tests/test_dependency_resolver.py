from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.cli import main
from agent.dependency_resolver import (
    resolve_task_dependencies,
    select_dependency_task,
)
from agent.execution.errors import TaskSelectionError
from agent.execution.executor import execute_task
from knowledge.registry import load_rule_registry
from llm.fake import FakeModelClient
from models.analyzer import (
    AnalyzedPackage,
    AnalyzerResult,
    GinUsage,
    SymbolReference,
)
from models.dependencies import (
    DependencyKind,
    DependencyResolution,
    TaskReadinessResult,
    TaskReadinessStatus,
)
from models.bundles import RepositoryMigrationPlan
from models.inspection import RepositoryInspection
from models.patch_proposal import PatchProposal
from tools.inspection import DEFAULT_RULE_PATH
from tools.migration_planner import build_migration_inventory
from tools.bundle_planner import build_repository_migration_plan

MODULE = "example.com/app"
GIN = "github.com/gin-gonic/gin"


def reference(
    symbol: str,
    *,
    package: str,
    file: str = "router.go",
    line: int = 8,
    definition_file: str = "",
    definition_line: int = 0,
    signature: str = "func()",
    framework_bound: bool = False,
    enclosing: str = f"{MODULE}.Routes",
    resolved: bool = True,
    standard_library: bool = False,
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
        definition_line=definition_line,
        signature=signature,
        framework_bound=framework_bound,
        standard_library=standard_library,
        resolved=resolved,
    )


def inspection_with(*references: SymbolReference) -> RepositoryInspection:
    builtin = reference(
        f"{GIN}.(*Engine).GET",
        package=GIN,
        framework_bound=True,
    )
    semantic_references = [builtin, *references]
    analysis = AnalyzerResult(
        module_path=MODULE,
        packages=[
            AnalyzedPackage(
                import_path=MODULE,
                name="app",
                source_files=["router.go"],
                imports=[GIN],
            )
        ],
        usages=[
            GinUsage(
                file="router.go",
                line=8,
                column=2,
                kind="engine_method",
                symbol=f"{GIN}.(*Engine).GET",
            )
        ],
        references=semantic_references,
        loading_errors=[],
    )
    inventory = build_migration_inventory(
        analysis,
        load_rule_registry(DEFAULT_RULE_PATH),
    )
    return RepositoryInspection.from_inventory(
        ".",
        [MODULE],
        inventory,
        semantic_references,
    )


def resolve(inspection: RepositoryInspection, *, max_depth: int = 8):
    task = select_dependency_task(inspection, phase="routing")
    return resolve_task_dependencies(
        inspection,
        task,
        load_rule_registry(DEFAULT_RULE_PATH),
        max_depth=max_depth,
    )


def test_local_handler_and_middleware_create_stable_unique_child_tasks() -> None:
    handler = reference(
        f"{MODULE}/api.Get",
        package=f"{MODULE}/api",
        definition_file="api/get.go",
        definition_line=10,
        signature=f"func(*{GIN}.Context)",
        framework_bound=True,
    )
    middleware = reference(
        f"{MODULE}/middleware.JWT",
        package=f"{MODULE}/middleware",
        definition_file="middleware/jwt.go",
        definition_line=12,
        signature=f"func() {GIN}.HandlerFunc",
        framework_bound=True,
    )
    result = resolve(inspection_with(handler, handler, middleware, middleware))
    second = resolve(inspection_with(handler, handler, middleware, middleware))

    assert result.status == TaskReadinessStatus.BLOCKED_LOCAL_DEPENDENCIES
    assert len(result.generated_child_tasks) == 2
    assert result.generated_child_tasks == second.generated_child_tasks
    assert result.dependency_graph == second.dependency_graph
    assert {task.phase.value for task in result.generated_child_tasks} == {
        "handler_signatures",
        "middleware",
    }
    assert all(
        dependency.resolution == DependencyResolution.CREATE_CHILD_TASK
        for dependency in result.local_dependencies
    )


def test_framework_independent_local_external_and_standard_library() -> None:
    local = reference(
        f"{MODULE}/token.Parse",
        package=f"{MODULE}/token",
        definition_file="token/parse.go",
    )
    external = reference(
        "github.com/golang-jwt/jwt.Parse",
        package="github.com/golang-jwt/jwt",
    )
    standard = reference(
        "strings.TrimSpace",
        package="strings",
        standard_library=True,
    )
    result = resolve(inspection_with(local, external, standard))

    assert result.ready
    assert result.local_dependencies[0].kind == (
        DependencyKind.LOCAL_FRAMEWORK_INDEPENDENT
    )
    assert result.local_dependencies[0].resolution == DependencyResolution.NO_ACTION
    assert result.external_dependencies[0].symbol == external.symbol
    assert all(item.symbol != standard.symbol for item in result.external_dependencies)
    assert result.generated_child_tasks == []


def test_external_framework_dependency_with_rule_generates_replacement() -> None:
    cors = reference(
        "github.com/gin-contrib/cors.Default",
        package="github.com/gin-contrib/cors",
        signature=f"func() {GIN}.HandlerFunc",
        framework_bound=True,
    )
    result = resolve(inspection_with(cors))

    dependency = result.external_dependencies[0]
    assert dependency.resolution == DependencyResolution.REPLACE_DEPENDENCY
    assert dependency.replacement_package == "github.com/hertz-contrib/cors"
    assert dependency.matching_rule_ids == ["gin-contrib.cors.default"]
    assert result.generated_child_tasks[0].phase.value == "dependencies"
    assert result.generated_child_tasks[0].files == ["router.go"]


def test_unknown_external_framework_dependency_blocks() -> None:
    unknown = reference(
        "company.example/gin-sdk.New",
        package="company.example/gin-sdk",
        signature=f"func() {GIN}.HandlerFunc",
        framework_bound=True,
    )
    result = resolve(inspection_with(unknown))

    assert result.status == TaskReadinessStatus.BLOCKED_EXTERNAL_DEPENDENCY
    assert result.blocking_dependencies[0].resolution == DependencyResolution.BLOCK
    assert result.generated_child_tasks == []


def test_unresolved_symbol_blocks() -> None:
    unresolved = reference(
        "missingHandler",
        package="",
        resolved=False,
    )
    result = resolve(inspection_with(unresolved))

    assert result.status == TaskReadinessStatus.BLOCKED_UNRESOLVED_DEPENDENCY
    assert result.unresolved_dependencies[0].symbol == "missingHandler"


def test_missing_semantic_reference_data_fails_closed() -> None:
    inspection = inspection_with().model_copy(
        update={"semantic_references": []}
    )

    result = resolve(inspection)

    assert result.status == TaskReadinessStatus.BLOCKED_UNRESOLVED_DEPENDENCY
    assert result.unresolved_dependencies[0].symbol == (
        "<semantic dependency data unavailable>"
    )
    assert "regenerate the plan with the current Go analyzer" in (
        result.unresolved_dependencies[0].notes
    )


def test_gin_builtin_uses_exact_registry_rule() -> None:
    result = resolve(inspection_with())

    assert result.ready
    assert result.framework_builtins[0].resolution == (
        DependencyResolution.USE_MIGRATION_RULE
    )
    assert result.framework_builtins[0].matching_rule_ids == ["gin.engine.get"]


@pytest.mark.parametrize(
    ("symbol", "rule_id", "replacement_package"),
    [
        (
            f"{GIN}.Logger",
            "gin.logger",
            "github.com/hertz-contrib/logger/accesslog",
        ),
        (
            f"{GIN}.Recovery",
            "gin.recovery",
            "github.com/cloudwego/hertz/pkg/app/middlewares/server/recovery",
        ),
        (
            f"{GIN}.(*Engine).StaticFS",
            "gin.engine.staticfs",
            "github.com/cloudwego/hertz/pkg/common/adaptor",
        ),
        (
            f"{GIN}.(*Context).Abort",
            "gin.context.abort",
            "github.com/cloudwego/hertz/pkg/app",
        ),
        (
            f"{GIN}.(*Context).PostForm",
            "gin.context.post-form",
            "github.com/cloudwego/hertz/pkg/app",
        ),
        (
            f"{GIN}.(*Context).SaveUploadedFile",
            "gin.context.save-uploaded-file",
            "github.com/cloudwego/hertz/pkg/app",
        ),
        (
            f"{GIN}.(*Context).Bind",
            "gin.context.bind",
            "github.com/cloudwego/hertz/pkg/app",
        ),
    ],
)
def test_documented_gin_builtins_use_registry_rules(
    symbol: str,
    rule_id: str,
    replacement_package: str,
) -> None:
    builtin = reference(
        symbol,
        package=GIN,
        framework_bound=True,
    )

    result = resolve(inspection_with(builtin))
    matched = next(
        dependency
        for dependency in result.framework_builtins
        if dependency.symbol == symbol
    )

    assert matched.resolution == DependencyResolution.USE_MIGRATION_RULE
    assert matched.matching_rule_ids == [rule_id]
    assert matched.replacement_package == replacement_package


def test_documented_swagger_adaptor_rule_generates_replacement_task() -> None:
    swagger = reference(
        "github.com/swaggo/gin-swagger.WrapHandler",
        package="github.com/swaggo/gin-swagger",
        signature=f"func(any) {GIN}.HandlerFunc",
        framework_bound=True,
    )

    result = resolve(inspection_with(swagger))
    dependency = result.external_dependencies[0]

    assert dependency.resolution == DependencyResolution.REPLACE_DEPENDENCY
    assert dependency.matching_rule_ids == ["gin-swagger.wrap-handler"]
    assert dependency.replacement_package == (
        "github.com/cloudwego/hertz/pkg/common/adaptor"
    )
    assert result.generated_child_tasks[0].files == ["router.go"]


def test_cycles_and_maximum_depth_are_deterministically_reported() -> None:
    first = reference(
        f"{MODULE}/middleware.A",
        package=f"{MODULE}/middleware",
        definition_file="middleware/a.go",
        definition_line=1,
        framework_bound=True,
    )
    second = reference(
        f"{MODULE}/middleware.B",
        package=f"{MODULE}/middleware",
        file="middleware/a.go",
        line=2,
        definition_file="middleware/b.go",
        definition_line=1,
        framework_bound=True,
        enclosing=first.symbol,
    )
    cycle = reference(
        first.symbol,
        package=f"{MODULE}/middleware",
        file="middleware/b.go",
        line=2,
        definition_file="middleware/a.go",
        definition_line=1,
        framework_bound=True,
        enclosing=second.symbol,
    )
    cyclic = resolve(inspection_with(first, second, cycle))
    shallow = resolve(inspection_with(first, second), max_depth=1)

    assert cyclic.dependency_graph.cycles
    assert cyclic.status == TaskReadinessStatus.BLOCKED_UNRESOLVED_DEPENDENCY
    assert shallow.dependency_graph.max_depth_reached
    assert shallow.status == TaskReadinessStatus.BLOCKED_UNRESOLVED_DEPENDENCY


def test_only_canonical_phases_are_accepted() -> None:
    inspection = inspection_with()
    with pytest.raises(TaskSelectionError, match="invalid migration phase"):
        select_dependency_task(inspection, phase="made_up")


def test_blocked_executor_never_calls_model_and_repository_is_unchanged(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    source = repository / "router.go"
    source.write_text("package app\n", encoding="utf-8")
    before = source.read_bytes()
    unresolved = reference("missingHandler", package="", resolved=False)
    inspection = inspection_with(unresolved)
    task = select_dependency_task(inspection, phase="routing")
    proposal = PatchProposal(
        task_id="unused",
        status="no_change_needed",
        summary="unused",
        files_to_modify=[],
        patch=None,
        confidence="low",
        assumptions=[],
        manual_review_notes=[],
        unresolved_dependencies=[],
    )
    model = FakeModelClient(proposal)

    result = execute_task(
        str(repository),
        inspection,
        load_rule_registry(DEFAULT_RULE_PATH),
        model,
        phase="routing",
    )

    assert isinstance(result, TaskReadinessResult)
    assert not result.ready
    assert model.calls == []
    assert source.read_bytes() == before
    assert task.phase.value == "routing"


def test_ready_executor_calls_existing_model(tmp_path: Path) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    (repository / "router.go").write_text(
        "package app\n\nfunc Routes(r *gin.Engine) {\n\n\n\n\n"
        'r.GET(\"/\", handler)\n}\n',
        encoding="utf-8",
    )
    inspection = inspection_with()
    task = select_dependency_task(inspection, phase="routing")
    proposal = PatchProposal(
        task_id=f"migration-task-{task.order:02d}-routing",
        status="blocked",
        summary="test model was called",
        files_to_modify=[],
        patch=None,
        confidence="low",
        assumptions=[],
        manual_review_notes=[],
        unresolved_dependencies=[],
    )
    model = FakeModelClient(proposal)

    result = execute_task(
        str(repository),
        inspection,
        load_rule_registry(DEFAULT_RULE_PATH),
        model,
        phase="routing",
    )

    assert not isinstance(result, TaskReadinessResult)
    assert len(model.calls) == 1


def test_dependency_cli_outputs_json_and_graph(
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    inspection = inspection_with()
    plan = tmp_path / "plan.json"
    plan.write_text(inspection.model_dump_json(), encoding="utf-8")
    stdout = io.StringIO()
    stderr = io.StringIO()

    exit_code = main(
        [
            "dependency",
            "resolve",
            "--repo",
            str(repository),
            "--plan",
            str(plan),
            "--phase",
            "routing",
            "--show-graph",
        ],
        stdout=stdout,
        stderr=stderr,
    )

    assert exit_code == 0
    assert "Readiness: ready" in stdout.getvalue()
    assert "Dependency graph:" in stdout.getvalue()
    assert '"dependency_graph"' in stdout.getvalue()
    assert stderr.getvalue() == ""


def test_repository_bundle_plan_is_deterministic_and_dependency_first() -> None:
    handler = reference(
        f"{MODULE}/api.Get",
        package=f"{MODULE}/api",
        definition_file="api/get.go",
        definition_line=10,
        signature=f"func(*{GIN}.Context)",
        framework_bound=True,
    )
    independent = reference(
        f"{MODULE}/service.Find",
        package=f"{MODULE}/service",
        definition_file="service/find.go",
    )
    cors = reference(
        "github.com/gin-contrib/cors.Default",
        package="github.com/gin-contrib/cors",
        signature=f"func() {GIN}.HandlerFunc",
        framework_bound=True,
    )
    inspection = inspection_with(handler, handler, independent, cors)
    registry = load_rule_registry(DEFAULT_RULE_PATH)

    first = build_repository_migration_plan(inspection, registry)
    second = build_repository_migration_plan(inspection, registry)

    assert first == second
    assert len(first.bundles) == 2
    assert len({bundle.bundle_id for bundle in first.bundles}) == 2
    assert all("service/find.go" not in bundle.files for bundle in first.bundles)
    assert sum(
        symbol == handler.symbol
        for bundle in first.bundles
        for symbol in bundle.framework_symbols
    ) == 1
    router_bundle = next(
        bundle for bundle in first.bundles if "router.go" in bundle.files
    )
    handler_bundle = next(
        bundle for bundle in first.bundles if "api/get.go" in bundle.files
    )
    assert router_bundle.depends_on == [handler_bundle.bundle_id]
    assert first.ready_bundle_ordering.index(handler_bundle.bundle_id) < (
        first.ready_bundle_ordering.index(router_bundle.bundle_id)
    )
    assert router_bundle.external_dependencies[0].resolution == (
        DependencyResolution.REPLACE_DEPENDENCY
    )


def test_repository_bundle_plan_detects_cycles_and_external_blockers() -> None:
    first = reference(
        f"{MODULE}/a.A",
        package=f"{MODULE}/a",
        definition_file="a/a.go",
        definition_line=1,
        framework_bound=True,
    )
    second = reference(
        f"{MODULE}.Routes",
        package=MODULE,
        file="a/a.go",
        line=2,
        definition_file="router.go",
        definition_line=1,
        framework_bound=True,
        enclosing=first.symbol,
    )
    unknown = reference(
        "company.example/gin-sdk.New",
        package="company.example/gin-sdk",
        signature=f"func() {GIN}.HandlerFunc",
        framework_bound=True,
    )

    plan = build_repository_migration_plan(
        inspection_with(first, second, unknown),
        load_rule_registry(DEFAULT_RULE_PATH),
    )

    assert plan.dependency_graph.cycles
    assert any(bundle.blocking_dependencies for bundle in plan.bundles)
    assert any(
        dependency.symbol == unknown.symbol
        for bundle in plan.bundles
        for dependency in bundle.blocking_dependencies
    )
    blocked_ids = {
        bundle.bundle_id for bundle in plan.bundles if bundle.blocking_dependencies
    }
    assert blocked_ids.isdisjoint(plan.ready_bundle_ordering)


@patch("agent.bundle_cli.inspect_repository")
def test_bundle_cli_writes_plan_without_modifying_repository(
    inspect,
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    source = repository / "router.go"
    source.write_bytes(b"package app\n")
    before = source.read_bytes()
    inspect.return_value = inspection_with()
    output = tmp_path / "bundle-plan.json"
    stdout = io.StringIO()

    exit_code = main(
        [
            "bundle",
            "plan",
            "--repo",
            str(repository),
            "--output",
            str(output),
            "--show-graph",
        ],
        stdout=stdout,
        stderr=io.StringIO(),
    )

    assert exit_code == 0
    plan = RepositoryMigrationPlan.model_validate_json(
        output.read_text(encoding="utf-8")
    )
    assert plan.bundles
    assert "Bundle dependency graph:" in stdout.getvalue()
    assert source.read_bytes() == before


@patch("agent.patch_cli.create_model_client")
def test_blocked_patch_command_does_not_initialize_or_call_gemini(
    create_client,
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    unknown = reference(
        "company.example/gin-sdk.New",
        package="company.example/gin-sdk",
        signature=f"func() {GIN}.HandlerFunc",
        framework_bound=True,
    )
    inspection = inspection_with(unknown)
    plan = tmp_path / "plan.json"
    plan.write_text(inspection.model_dump_json(), encoding="utf-8")
    stdout = io.StringIO()

    exit_code = main(
        [
            "patch",
            "generate",
            "--repo",
            str(repository),
            "--plan",
            str(plan),
            "--phase",
            "routing",
        ],
        stdout=stdout,
        stderr=io.StringIO(),
    )

    assert exit_code == 1
    assert "blocked_external_dependency" in stdout.getvalue()
    assert "company.example/gin-sdk.New" in stdout.getvalue()
    assert "no deterministic dependency replacement rule" in stdout.getvalue()
    assert "Hard blocking dependencies: 1" in stdout.getvalue()
    assert "Patch proposal was not generated." in stdout.getvalue()
    create_client.assert_not_called()


@patch("agent.patch_cli.create_model_client")
def test_patch_command_explains_outdated_plan(
    create_client,
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    inspection = inspection_with().model_copy(
        update={"semantic_references": []}
    )
    plan = tmp_path / "old-plan.json"
    plan.write_text(inspection.model_dump_json(), encoding="utf-8")
    stdout = io.StringIO()

    exit_code = main(
        [
            "patch",
            "generate",
            "--repo",
            str(repository),
            "--plan",
            str(plan),
            "--phase",
            "routing",
        ],
        stdout=stdout,
        stderr=io.StringIO(),
    )

    output = stdout.getvalue()
    assert exit_code == 1
    assert "<semantic dependency data unavailable>" in output
    assert "regenerate the plan with the current Go analyzer" in output
    assert '"dependency_graph"' not in output
    assert '"unresolved_dependencies"' not in output
    create_client.assert_not_called()


@patch("agent.patch_cli.create_model_client")
def test_blocked_patch_preserves_and_identifies_existing_output(
    create_client,
    tmp_path: Path,
) -> None:
    repository = tmp_path / "repo"
    repository.mkdir()
    inspection = inspection_with().model_copy(
        update={"semantic_references": []}
    )
    plan = tmp_path / "plan.json"
    plan.write_text(inspection.model_dump_json(), encoding="utf-8")
    output = tmp_path / "routing-proposal.json"
    original = b'{"old": true}\n'
    output.write_bytes(original)
    stdout = io.StringIO()

    exit_code = main(
        [
            "patch",
            "generate",
            "--repo",
            str(repository),
            "--plan",
            str(plan),
            "--phase",
            "routing",
            "--output",
            str(output),
        ],
        stdout=stdout,
        stderr=io.StringIO(),
    )

    assert exit_code == 1
    assert output.read_bytes() == original
    assert "Patch proposal was not written:" in stdout.getvalue()
    assert "existing output file was left unchanged" in stdout.getvalue()
    create_client.assert_not_called()
