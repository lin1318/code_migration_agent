import io
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from agent.cli import main
from agent.execution.context_builder import (
    build_task_context,
    select_task,
    stable_task_id,
)
from agent.execution.errors import (
    ModelInvocationError,
    ProposalValidationError,
    SuspiciousNoChangeProposalError,
    TaskSelectionError,
)
from agent.execution.prompt_builder import build_prompt
from agent.execution.service import execute_migration_task
from agent.execution.validator import validate_proposal
from knowledge.registry import load_rule_registry
from llm.aihubmix import AiHubMixModelClient
from llm.fake import FakeModelClient
from llm.factory import create_model_client
from models.analyzer import (
    AnalyzedPackage,
    AnalyzerResult,
    GinUsage,
    SymbolReference,
)
from models.inspection import RepositoryInspection
from models.patch_proposal import (
    PatchConfidence,
    PatchProposal,
    PatchProposalStatus,
)
from models.workflow import WorkflowReport
from tools.inspection import DEFAULT_RULE_PATH
from tools.migration_planner import build_migration_inventory


def write_repository(repository: Path) -> None:
    router = repository / "routers" / "router.go"
    handler = repository / "routers" / "api" / "v1" / "tag.go"
    unrelated = repository / "models" / "tag.go"
    router.parent.mkdir(parents=True)
    handler.parent.mkdir(parents=True)
    unrelated.parent.mkdir(parents=True)
    router.write_text(
        "package routers\n"
        "\n"
        "import (\n"
        '    "github.com/gin-gonic/gin"\n'
        '    v1 "example.com/shop/routers/api/v1"\n'
        ")\n"
        "\n"
        "func InitRouter(r *gin.Engine) {\n"
        '    r.GET("/tags", v1.GetTags)\n'
        "}\n",
        encoding="utf-8",
    )
    handler.write_text(
        "package v1\n"
        "\n"
        "func GetTags(c *gin.Context) {\n"
        "    c.JSON(200, gin.H{})\n"
        "}\n",
        encoding="utf-8",
    )
    unrelated.write_text(
        "package models\n\ntype Tag struct{}\n",
        encoding="utf-8",
    )


def build_inspection(repository: Path) -> RepositoryInspection:
    analysis = AnalyzerResult(
        module_path="example.com/shop",
        packages=[
            AnalyzedPackage(
                import_path="example.com/shop/routers",
                name="routers",
                source_files=["routers/router.go"],
                imports=["github.com/gin-gonic/gin"],
            )
        ],
        usages=[
            GinUsage(
                file="routers/router.go",
                line=4,
                column=8,
                kind="gin_import",
                symbol="github.com/gin-gonic/gin",
            ),
            GinUsage(
                file="routers/router.go",
                line=9,
                column=5,
                kind="engine_method",
                symbol="github.com/gin-gonic/gin.(*Engine).GET",
            ),
        ],
        references=[
            SymbolReference(
                file="routers/router.go",
                line=9,
                column=5,
                enclosing_symbol="example.com/shop/routers.InitRouter",
                symbol="github.com/gin-gonic/gin.(*Engine).GET",
                package_path="github.com/gin-gonic/gin",
                module_path="github.com/gin-gonic/gin",
                signature="func(string, ...gin.HandlerFunc)",
                framework_bound=True,
                resolved=True,
            )
        ],
        loading_errors=[],
    )
    inventory = build_migration_inventory(
        analysis,
        load_rule_registry(DEFAULT_RULE_PATH),
    )
    return RepositoryInspection.from_inventory(
        str(repository),
        ["example.com/shop/routers"],
        inventory,
        analysis.references,
    )


@pytest.fixture
def execution_fixture(tmp_path: Path):
    repository = tmp_path / "repository"
    repository.mkdir()
    write_repository(repository)
    inspection = build_inspection(repository)
    plan = tmp_path / "migration-plan.json"
    plan.write_text(inspection.model_dump_json(indent=2), encoding="utf-8")
    return repository, inspection, plan


def generated_proposal(task_id: str) -> PatchProposal:
    return PatchProposal(
        task_id=task_id,
        status=PatchProposalStatus.PATCH_GENERATED,
        summary="Replace the routing receiver while preserving the route.",
        files_to_modify=["routers/router.go"],
        patch=(
            "diff --git a/routers/router.go b/routers/router.go\n"
            "--- a/routers/router.go\n"
            "+++ b/routers/router.go\n"
            "@@ -5,3 +5,3 @@\n"
            " func InitRouter(r *gin.Engine) {\n"
            '-    r.GET(\"/tags\", v1.GetTags)\n'
            '+    h.GET(\"/tags\", v1.GetTags)\n'
            " }\n"
        ),
        confidence=PatchConfidence.MEDIUM,
        assumptions=["The Hertz server receiver is named h."],
        manual_review_notes=["Handler migration is a later task."],
        unresolved_dependencies=["server_bootstrap"],
    )


def snapshot(repository: Path) -> dict[str, bytes]:
    return {
        path.relative_to(repository).as_posix(): path.read_bytes()
        for path in repository.rglob("*")
        if path.is_file()
    }


def test_task_selection_supports_exactly_one_routing_task(
    execution_fixture,
) -> None:
    _, inspection, _ = execution_fixture

    task = select_task(inspection, phase="routing")

    assert task.phase.value == "routing"
    assert task.direct_usage_ids
    assert stable_task_id(task) == "migration-task-03-routing"
    with pytest.raises(TaskSelectionError, match="only the routing phase"):
        select_task(inspection, phase="responses")
    with pytest.raises(TaskSelectionError, match="found 0"):
        select_task(inspection, phase="routing", task_order=9)


def test_context_contains_only_task_relevant_repository_data(
    execution_fixture,
) -> None:
    repository, inspection, _ = execution_fixture
    task = select_task(inspection, phase="routing")

    context = build_task_context(
        repository,
        inspection,
        task,
        load_rule_registry(DEFAULT_RULE_PATH),
    )

    assert context.allowed_files == ["routers/router.go"]
    assert context.migration_task_id == "migration-task-03-routing"
    assert len(context.direct_usages) == 1
    assert context.direct_usages[0].symbol.endswith("(*Engine).GET")
    assert [rule.rule_id for rule in context.matching_migration_rules] == [
        "gin.engine.get"
    ]
    assert [source.path for source in context.relevant_source_files] == [
        "routers/router.go"
    ]
    assert all(
        source.path != "models/tag.go"
        for source in context.relevant_source_files
    )
    assert context.relevant_line_ranges[0].path == "routers/router.go"
    assert any(
        definition.symbol == "v1.GetTags"
        and definition.path == "routers/api/v1/tag.go"
        and "func GetTags" in definition.signature
        for definition in context.related_symbol_definitions
    )
    assert context.completed_migration_phases == []
    assert [dependency.phase for dependency in context.pending_dependencies] == [
        "dependencies",
        "server_bootstrap",
    ]


def test_prompt_is_loaded_from_template(execution_fixture) -> None:
    repository, inspection, _ = execution_fixture
    task = select_task(inspection, phase="routing")
    context = build_task_context(
        repository,
        inspection,
        task,
        load_rule_registry(DEFAULT_RULE_PATH),
    )

    prompt = build_prompt(context)

    assert "preserve existing behavior" in prompt.lower()
    assert "Never invent Hertz APIs" in prompt
    assert '"allowed_files": [' in prompt
    assert "routers/router.go" in prompt
    assert "models/tag.go" not in prompt
    assert "not proof" in prompt
    assert "that their changes were applied" in prompt
    assert "engine and router-group types" in prompt


def test_fake_model_is_deterministic(execution_fixture) -> None:
    repository, inspection, plan = execution_fixture
    task = select_task(inspection, phase="routing")
    proposal = generated_proposal(stable_task_id(task))
    model = FakeModelClient(proposal)

    first = execute_migration_task(
        repository,
        plan,
        model,
        phase="routing",
    )
    second = execute_migration_task(
        repository,
        plan,
        model,
        phase="routing",
    )

    assert first == second
    assert first.model_used == "fake-deterministic"
    assert first.context_summary.allowed_file_count == 1
    assert first.context_summary.related_symbol_count == 1
    assert len(model.calls) == 2
    assert model.calls[0][1] == model.calls[1][1]


def test_execution_returns_proposal_without_modifying_repository(
    execution_fixture,
) -> None:
    repository, inspection, plan = execution_fixture
    task = select_task(inspection, phase="routing")
    before = snapshot(repository)

    result = execute_migration_task(
        repository,
        plan,
        FakeModelClient(generated_proposal(stable_task_id(task))),
        phase="routing",
    )

    assert result.proposal.status == PatchProposalStatus.PATCH_GENERATED
    assert snapshot(repository) == before


def test_service_accepts_successful_workflow_report(
    execution_fixture,
    tmp_path: Path,
) -> None:
    repository, inspection, _ = execution_fixture
    task = select_task(inspection, phase="routing")
    workflow_plan = tmp_path / "workflow-plan.json"
    workflow_plan.write_text(
        WorkflowReport(
            **inspection.model_dump(),
            completed_stages=[
                "validate_input",
                "inspect_repository",
                "load_migration_rules",
                "build_inventory",
                "validate_inventory",
                "build_plan",
                "validate_plan",
                "generate_report",
            ],
            warnings=[],
            errors=[],
            final_status="success",
        ).model_dump_json(indent=2),
        encoding="utf-8",
    )

    result = execute_migration_task(
        repository,
        workflow_plan,
        FakeModelClient(generated_proposal(stable_task_id(task))),
        phase="routing",
    )

    assert result.selected_task == task


def test_blocked_and_no_change_proposals() -> None:
    blocked = PatchProposal(
        task_id="migration-task-03-routing",
        status="blocked",
        summary="The server receiver name is unavailable.",
        files_to_modify=[],
        patch=None,
        confidence="low",
        assumptions=[],
        manual_review_notes=["Supply bootstrap output."],
        unresolved_dependencies=["server_bootstrap"],
    )
    no_change = blocked.model_copy(
        update={
            "status": PatchProposalStatus.NO_CHANGE_NEEDED,
            "summary": "Routing already uses Hertz.",
            "manual_review_notes": [],
            "unresolved_dependencies": [],
        }
    )

    assert validate_proposal(
        blocked,
        expected_task_id=blocked.task_id,
        allowed_files={"routers/router.go"},
    ) == blocked
    assert validate_proposal(
        no_change,
        expected_task_id=no_change.task_id,
        allowed_files={"routers/router.go"},
    ) == no_change


def test_same_route_method_on_different_framework_requires_a_change(
    execution_fixture,
) -> None:
    repository, inspection, _ = execution_fixture
    task = select_task(inspection, phase="routing")
    context = build_task_context(
        repository,
        inspection,
        task,
        load_rule_registry(DEFAULT_RULE_PATH),
    )
    proposal = PatchProposal(
        task_id=stable_task_id(task),
        status="no_change_needed",
        summary="GET has the same spelling in both frameworks.",
        files_to_modify=[],
        patch=None,
        confidence="high",
        assumptions=[],
        manual_review_notes=[],
        unresolved_dependencies=[],
    )

    with pytest.raises(SuspiciousNoChangeProposalError) as captured:
        validate_proposal(
            proposal,
            expected_task_id=proposal.task_id,
            allowed_files=set(context.allowed_files),
            context=context,
        )

    assert captured.value.details.operation == "validate_no_change_needed"
    assert captured.value.details.error_type == "SuspiciousNoChangeProposalError"
    assert captured.value.details.diagnostics["files"] == "routers/router.go"


def test_text_compatible_rule_may_return_no_change(
    execution_fixture,
) -> None:
    repository, inspection, _ = execution_fixture
    task = select_task(inspection, phase="routing")
    context = build_task_context(
        repository,
        inspection,
        task,
        load_rule_registry(DEFAULT_RULE_PATH),
    )
    original_rule = context.matching_migration_rules[0]
    compatible_rule = original_rule.model_copy(
        update={
            "target_package": original_rule.source_package,
            "target_symbol": original_rule.source_symbol,
        }
    )
    compatible_context = context.model_copy(
        update={"matching_migration_rules": [compatible_rule]}
    )
    proposal = PatchProposal(
        task_id=stable_task_id(task),
        status="no_change_needed",
        summary="The source and target syntax are identical.",
        files_to_modify=[],
        patch=None,
        confidence="high",
        assumptions=[],
        manual_review_notes=[],
        unresolved_dependencies=[],
    )

    assert validate_proposal(
        proposal,
        expected_task_id=proposal.task_id,
        allowed_files=set(context.allowed_files),
        context=compatible_context,
    ) == proposal


def test_previous_plan_phases_are_not_treated_as_applied(
    execution_fixture,
) -> None:
    repository, inspection, _ = execution_fixture
    task = select_task(inspection, phase="routing")
    context = build_task_context(
        repository,
        inspection,
        task,
        load_rule_registry(DEFAULT_RULE_PATH),
        completed_phases=["dependencies", "server_bootstrap"],
    )
    proposal = PatchProposal(
        task_id=stable_task_id(task),
        status="no_change_needed",
        summary="Earlier phases appear before routing.",
        files_to_modify=[],
        patch=None,
        confidence="high",
        assumptions=[],
        manual_review_notes=[],
        unresolved_dependencies=[],
    )

    with pytest.raises(SuspiciousNoChangeProposalError):
        validate_proposal(
            proposal,
            expected_task_id=proposal.task_id,
            allowed_files=set(context.allowed_files),
            context=context,
        )
    assert "github.com/gin-gonic/gin" in context.relevant_source_files[0].content


def test_explicit_updated_source_may_be_treated_as_applied(
    execution_fixture,
) -> None:
    repository, inspection, _ = execution_fixture
    task = select_task(inspection, phase="routing")
    updated_source = (
        "package routers\n"
        "\n"
        "import (\n"
        '    "github.com/cloudwego/hertz/pkg/app/server"\n'
        '    v1 "example.com/shop/routers/api/v1"\n'
        ")\n"
        "\n"
        "func InitRouter(h *server.Hertz) {\n"
        '    h.GET("/tags", v1.GetTags)\n'
        "}\n"
    )
    context = build_task_context(
        repository,
        inspection,
        task,
        load_rule_registry(DEFAULT_RULE_PATH),
        completed_phases=["dependencies", "server_bootstrap"],
        updated_source_content={"routers/router.go": updated_source},
    )
    proposal = PatchProposal(
        task_id=stable_task_id(task),
        status="no_change_needed",
        summary="Explicit updated source already uses Hertz.",
        files_to_modify=[],
        patch=None,
        confidence="high",
        assumptions=[],
        manual_review_notes=[],
        unresolved_dependencies=[],
    )

    assert validate_proposal(
        proposal,
        expected_task_id=proposal.task_id,
        allowed_files=set(context.allowed_files),
        context=context,
    ) == proposal
    assert context.relevant_source_files[0].content == updated_source


def test_blocked_proposal_may_include_a_valid_partial_patch() -> None:
    generated = generated_proposal("migration-task-03-routing")
    blocked = generated.model_copy(
        update={
            "status": PatchProposalStatus.BLOCKED,
            "summary": "A partial routing patch needs bootstrap review.",
            "unresolved_dependencies": ["server_bootstrap"],
        }
    )

    assert validate_proposal(
        blocked,
        expected_task_id=blocked.task_id,
        allowed_files={"routers/router.go"},
    ) == blocked


def test_invalid_status_payload_is_rejected() -> None:
    with pytest.raises(ValidationError, match="non-empty patch"):
        PatchProposal(
            task_id="migration-task-03-routing",
            status="patch_generated",
            summary="Missing patch.",
            files_to_modify=["routers/router.go"],
            patch="",
            confidence="low",
            assumptions=[],
            manual_review_notes=[],
            unresolved_dependencies=[],
        )
    with pytest.raises(ValidationError, match="null patch"):
        PatchProposal(
            task_id="migration-task-03-routing",
            status="no_change_needed",
            summary="No change.",
            files_to_modify=[],
            patch="unexpected",
            confidence="high",
            assumptions=[],
            manual_review_notes=[],
            unresolved_dependencies=[],
        )


@pytest.mark.parametrize(
    ("patch_transform", "message"),
    [
        (lambda value: f"```diff\n{value}```", "code fences"),
        (
            lambda value: value.replace(
                "a/routers/router.go",
                "a/C:/router.go",
            ).replace("b/routers/router.go", "b/C:/router.go"),
            "unsafe patch path",
        ),
        (
            lambda value: value.replace(
                "routers/router.go",
                "../router.go",
            ),
            "unsafe patch path",
        ),
        (
            lambda value: value.replace(
                "routers/router.go",
                ".git/config",
            ),
            "unsafe patch path",
        ),
        (
            lambda _value: (
                "diff --git a/routers/router.go b/routers/router.go\n"
                "GIT binary patch\nliteral 3\n"
            ),
            "binary patches",
        ),
        (
            lambda _value: (
                "diff --git a/new.go b/new.go\n"
                "new file mode 100644\n"
                "--- /dev/null\n"
                "+++ b/new.go\n"
                "@@ -0,0 +1 @@\n+package routers\n"
            ),
            "new-file",
        ),
        (
            lambda _value: (
                "diff --git a/routers/router.go b/routers/new.go\n"
                "rename from routers/router.go\n"
                "rename to routers/new.go\n"
            ),
            "rename",
        ),
    ],
)
def test_patch_validation_rejects_unsafe_output(
    patch_transform,
    message: str,
) -> None:
    proposal = generated_proposal("migration-task-03-routing")
    invalid = proposal.model_copy(
        update={"patch": patch_transform(proposal.patch)}
    )

    with pytest.raises(ProposalValidationError, match=message):
        validate_proposal(
            invalid,
            expected_task_id=invalid.task_id,
            allowed_files={"routers/router.go"},
        )


def test_allowed_files_and_task_id_are_enforced() -> None:
    proposal = generated_proposal("wrong-task")
    with pytest.raises(ProposalValidationError, match="does not match"):
        validate_proposal(
            proposal,
            expected_task_id="migration-task-03-routing",
            allowed_files={"routers/router.go"},
        )
    outside = generated_proposal("migration-task-03-routing").model_copy(
        update={"files_to_modify": ["models/tag.go"]}
    )
    with pytest.raises(ProposalValidationError, match="outside allowed_files"):
        validate_proposal(
            outside,
            expected_task_id=outside.task_id,
            allowed_files={"routers/router.go"},
        )


def test_aihubmix_adapter_uses_structured_json_output(execution_fixture) -> None:
    repository, inspection, _ = execution_fixture
    task = select_task(inspection, phase="routing")
    context = build_task_context(
        repository,
        inspection,
        task,
        load_rule_registry(DEFAULT_RULE_PATH),
    )
    proposal = generated_proposal(stable_task_id(task))

    class Completions:
        def __init__(self) -> None:
            self.arguments = None

        def create(self, **kwargs):
            self.arguments = kwargs
            return SimpleNamespace(
                choices=[
                    SimpleNamespace(
                        message=SimpleNamespace(
                            content=proposal.model_dump_json(),
                            refusal=None,
                        )
                    )
                ]
            )

    completions = Completions()
    client = SimpleNamespace(
        chat=SimpleNamespace(completions=completions)
    )
    adapter = AiHubMixModelClient(client=client, model_name="gemini-test")

    result = adapter.generate_patch(context, "structured prompt")

    assert result == proposal
    assert completions.arguments["model"] == "gemini-test"
    assert completions.arguments["messages"] == [
        {"role": "user", "content": "structured prompt"}
    ]
    response_format = completions.arguments["response_format"]
    assert response_format["type"] == "json_schema"
    assert response_format["json_schema"]["strict"] is True
    assert (
        response_format["json_schema"]["schema"]["title"]
        == "PatchProposal"
    )


def test_aihubmix_adapter_rejects_invalid_structured_json(
    execution_fixture,
) -> None:
    repository, inspection, _ = execution_fixture
    task = select_task(inspection, phase="routing")
    context = build_task_context(
        repository,
        inspection,
        task,
        load_rule_registry(DEFAULT_RULE_PATH),
    )
    client = SimpleNamespace(
        chat=SimpleNamespace(
            completions=SimpleNamespace(
                create=lambda **_kwargs: SimpleNamespace(
                    choices=[
                        SimpleNamespace(
                            message=SimpleNamespace(
                                content='{"status":"invalid"}',
                                refusal=None,
                            )
                        )
                    ]
                )
            )
        )
    )

    with pytest.raises(ModelInvocationError, match="invalid structured"):
        AiHubMixModelClient(client=client).generate_patch(context, "prompt")


def test_aihubmix_requires_api_key(monkeypatch) -> None:
    monkeypatch.delenv("AIHUBMIX_API_KEY", raising=False)
    with patch("llm.aihubmix.load_project_environment"):
        with pytest.raises(ModelInvocationError, match="AIHUBMIX_API_KEY"):
            AiHubMixModelClient()


def test_model_factory_reads_aihubmix_environment(monkeypatch) -> None:
    monkeypatch.setenv("PATCH_MODEL_PROVIDER", "aihubmix")
    monkeypatch.setenv("PATCH_MODEL_NAME", "gemini-custom")

    with patch("llm.factory.AiHubMixModelClient") as client_type:
        client = create_model_client()

    assert client == client_type.return_value
    client_type.assert_called_once_with(model_name="gemini-custom")


def test_patch_generate_cli_with_fake_model(execution_fixture, tmp_path: Path) -> None:
    repository, inspection, plan = execution_fixture
    task = select_task(inspection, phase="routing")
    proposal = generated_proposal(stable_task_id(task))
    output = tmp_path / "proposal.json"
    stdout = io.StringIO()
    stderr = io.StringIO()

    with patch(
        "agent.patch_cli.create_model_client",
        return_value=FakeModelClient(proposal),
    ):
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
            stderr=stderr,
        )

    assert exit_code == 0
    assert stderr.getvalue() == ""
    assert "Selected task: migration-task-03-routing" in stdout.getvalue()
    assert "Model used: fake-deterministic" in stdout.getvalue()
    assert PatchProposal.model_validate_json(
        output.read_text(encoding="utf-8")
    ) == proposal


@pytest.mark.skipif(
    not os.getenv("AIHUBMIX_API_KEY"),
    reason="AIHUBMIX_API_KEY is not configured",
)
def test_optional_aihubmix_integration(execution_fixture) -> None:
    repository, _, plan = execution_fixture

    result = execute_migration_task(
        repository,
        plan,
        AiHubMixModelClient(),
        phase="routing",
    )

    assert result.proposal.task_id == "migration-task-03-routing"
