from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from agent.bundle_execution import (
    build_bundle_context,
    build_bundle_prompt,
    execute_bundle,
    select_ready_bundle,
    validate_bundle_proposal,
)
from agent.cli import main
from agent.execution.errors import (
    ModelInvocationError,
    ProposalValidationError,
    TaskSelectionError,
)
from knowledge.registry import load_rule_registry
from llm.aihubmix import AiHubMixModelClient
from llm.fake import FakeBundleModelClient
from llm.factory import create_bundle_model_client
from llm.gemini import DEFAULT_GEMINI_MODEL, GeminiModelClient
from models.analyzer import (
    AnalyzedPackage,
    AnalyzerResult,
    GinUsage,
    SymbolReference,
)
from models.bundle_execution import BundleFilePatch, BundlePatchProposal
from models.inspection import RepositoryInspection
from tools.bundle_planner import build_repository_migration_plan
from tools.inspection import DEFAULT_RULE_PATH
from tools.migration_planner import build_migration_inventory

GIN = "github.com/gin-gonic/gin"
MODULE = "example.com/bundle"


@pytest.fixture
def bundle_fixture(tmp_path: Path):
    repository = tmp_path / "repo"
    repository.mkdir()
    source = repository / "router.go"
    source.write_text(
        "package bundle\n\n"
        "func Routes(r *gin.Engine) {\n"
        '    r.GET("/ping", Ping)\n'
        "}\n",
        encoding="utf-8",
    )
    analysis = AnalyzerResult(
        module_path=MODULE,
        packages=[
            AnalyzedPackage(
                import_path=MODULE,
                name="bundle",
                source_files=["router.go"],
                imports=[GIN],
            )
        ],
        usages=[
            GinUsage(
                file="router.go",
                line=4,
                column=5,
                kind="engine_method",
                symbol=f"{GIN}.(*Engine).GET",
            )
        ],
        references=[
            SymbolReference(
                file="router.go",
                line=4,
                column=5,
                enclosing_symbol=f"{MODULE}.Routes",
                symbol=f"{GIN}.(*Engine).GET",
                package_path=GIN,
                module_path=GIN,
                definition_file="",
                definition_line=0,
                signature="func(string, ...gin.HandlerFunc)",
                framework_bound=True,
                resolved=True,
            )
        ],
        loading_errors=[],
    )
    registry = load_rule_registry(DEFAULT_RULE_PATH)
    inventory = build_migration_inventory(analysis, registry)
    inspection = RepositoryInspection.from_inventory(
        str(repository),
        [MODULE],
        inventory,
        analysis.references,
    )
    plan = build_repository_migration_plan(inspection, registry)
    bundle = select_ready_bundle(plan, plan.ready_bundle_ordering[0])
    return repository, inspection, plan, bundle, registry


def proposal(bundle_id: str) -> BundlePatchProposal:
    patch = (
        "diff --git a/router.go b/router.go\n"
        "--- a/router.go\n"
        "+++ b/router.go\n"
        "@@ -3,3 +3,3 @@\n"
        " func Routes(r *gin.Engine) {\n"
        '-    r.GET("/ping", Ping)\n'
        '+    h.GET("/ping", Ping)\n'
        " }\n"
    )
    return BundlePatchProposal(
        bundle_id=bundle_id,
        status="patch_generated",
        summary="Migrate the route receiver.",
        patches=[BundleFilePatch(file="router.go", patch=patch)],
        modified_files=["router.go"],
        confidence="medium",
        assumptions=[],
        manual_review_notes=[],
        unresolved_dependencies=[],
    )


def test_bundle_context_is_minimal_and_collects_rules(bundle_fixture) -> None:
    repository, inspection, plan, bundle, registry = bundle_fixture

    context = build_bundle_context(
        repository,
        inspection,
        plan,
        bundle,
        registry,
    )

    assert context.allowed_files == ["router.go"]
    assert [source.path for source in context.relevant_source_files] == [
        "router.go"
    ]
    assert context.relevant_source_ranges[0].path == "router.go"
    assert context.framework_symbols == [f"{GIN}.(*Engine).GET"]
    assert [rule.rule_id for rule in context.matching_migration_rules] == [
        "gin.engine.get"
    ]
    assert context.completed_bundles == []
    assert context.pending_bundles == []


def test_bundle_prompt_is_template_backed(bundle_fixture) -> None:
    repository, inspection, plan, bundle, registry = bundle_fixture
    context = build_bundle_context(
        repository, inspection, plan, bundle, registry
    )

    prompt = build_bundle_prompt(context)

    assert "Generate one migration bundle only" in prompt
    assert "Never invent Hertz APIs" in prompt
    assert "Unchanged context lines must be copied verbatim" in prompt
    assert '"allowed_files": [' in prompt
    assert "router.go" in prompt


def test_fake_bundle_execution_is_deterministic_and_read_only(
    bundle_fixture,
) -> None:
    repository, inspection, plan, bundle, registry = bundle_fixture
    model = FakeBundleModelClient(proposal(bundle.bundle_id))
    before = (repository / "router.go").read_bytes()

    first = execute_bundle(
        repository, inspection, plan, bundle, registry, model
    )
    second = execute_bundle(
        repository, inspection, plan, bundle, registry, model
    )

    assert first == second
    assert first.model_used == "fake-bundle-deterministic"
    assert len(first.proposal.patches) == 1
    assert len(model.calls) == 2
    assert (repository / "router.go").read_bytes() == before


def test_valid_and_blocked_bundle_proposals(bundle_fixture) -> None:
    _, _, _, bundle, _ = bundle_fixture
    generated = proposal(bundle.bundle_id)
    blocked = generated.model_copy(
        update={
            "status": "blocked",
            "patches": [],
            "modified_files": [],
            "summary": "A supplied rule requires review.",
        }
    )

    assert validate_bundle_proposal(generated, bundle) == generated
    assert validate_bundle_proposal(blocked, bundle) == blocked


def test_empty_patch_is_rejected() -> None:
    with pytest.raises(ValidationError, match="at least 1 character"):
        BundleFilePatch(file="router.go", patch="")


def test_duplicate_file_modification_is_rejected(bundle_fixture) -> None:
    _, _, _, bundle, _ = bundle_fixture
    generated = proposal(bundle.bundle_id)
    duplicate = generated.model_copy(
        update={"patches": [generated.patches[0], generated.patches[0]]}
    )

    with pytest.raises(ProposalValidationError, match="duplicate modifications"):
        validate_bundle_proposal(duplicate, bundle)


@pytest.mark.parametrize(
    ("file", "patch", "message"),
    [
        (
            "outside.go",
            "diff --git a/outside.go b/outside.go\n"
            "--- a/outside.go\n+++ b/outside.go\n"
            "@@ -1 +1 @@\n-old\n+new\n",
            "outside the bundle",
        ),
        (
            "router.go",
            "```diff\ndiff --git a/router.go b/router.go\n```",
            "Markdown code fences",
        ),
        (
            "router.go",
            "diff --git a/router.go b/router.go\nGIT binary patch\n",
            "binary patches",
        ),
    ],
)
def test_invalid_bundle_patches_are_rejected(
    bundle_fixture,
    file: str,
    patch: str,
    message: str,
) -> None:
    _, _, _, bundle, _ = bundle_fixture
    invalid = proposal(bundle.bundle_id).model_copy(
        update={
            "patches": [BundleFilePatch(file=file, patch=patch)],
            "modified_files": [file],
        }
    )

    with pytest.raises(ProposalValidationError, match=message):
        validate_bundle_proposal(invalid, bundle)


def test_modified_files_must_match_patch_files(bundle_fixture) -> None:
    _, _, _, bundle, _ = bundle_fixture
    invalid = proposal(bundle.bundle_id).model_copy(
        update={"modified_files": []}
    )

    with pytest.raises(ProposalValidationError, match="exactly match"):
        validate_bundle_proposal(invalid, bundle)


def test_patch_format_error_identifies_file_and_first_line(bundle_fixture) -> None:
    _, _, _, bundle, _ = bundle_fixture
    invalid = proposal(bundle.bundle_id).model_copy(
        update={
            "patches": [
                BundleFilePatch(
                    file="router.go",
                    patch="Here is the patch:\n--- a/router.go\n+++ b/router.go\n",
                )
            ]
        }
    )

    with pytest.raises(
        ProposalValidationError,
        match="invalid patch for 'router.go'.*Here is the patch",
    ):
        validate_bundle_proposal(invalid, bundle)


def test_incorrect_hunk_counts_are_rejected(bundle_fixture) -> None:
    _, _, _, bundle, _ = bundle_fixture
    generated = proposal(bundle.bundle_id)
    invalid_patch = generated.patches[0].patch.replace(
        "@@ -3,3 +3,3 @@",
        "@@ -3,9 +3,9 @@",
    )
    invalid = generated.model_copy(
        update={
            "patches": [BundleFilePatch(file="router.go", patch=invalid_patch)]
        }
    )

    with pytest.raises(ProposalValidationError, match="hunk line counts"):
        validate_bundle_proposal(invalid, bundle)


def test_execution_deterministically_corrects_model_hunk_counts(
    bundle_fixture,
) -> None:
    repository, inspection, plan, bundle, registry = bundle_fixture
    generated = proposal(bundle.bundle_id)
    wrong = generated.model_copy(
        update={
            "patches": [
                generated.patches[0].model_copy(
                    update={
                        "patch": generated.patches[0].patch.replace(
                            "@@ -3,3 +3,3 @@",
                            "@@ -3,12 +3,12 @@",
                        )
                    }
                )
            ]
        }
    )

    result = execute_bundle(
        repository,
        inspection,
        plan,
        bundle,
        registry,
        FakeBundleModelClient(wrong),
    )

    assert "@@ -3,3 +3,3 @@" in result.proposal.patches[0].patch


def test_execution_rejects_reordered_unchanged_patch_context(
    bundle_fixture,
) -> None:
    repository, inspection, plan, bundle, registry = bundle_fixture
    generated = proposal(bundle.bundle_id)
    invalid_patch = generated.patches[0].patch.replace(
        " func Routes(r *gin.Engine) {\n"
        '-    r.GET("/ping", Ping)\n',
        '-    r.GET("/ping", Ping)\n'
        " func Routes(r *gin.Engine) {\n",
    )
    invalid = generated.model_copy(
        update={
            "patches": [
                BundleFilePatch(file="router.go", patch=invalid_patch)
            ]
        }
    )

    with pytest.raises(
        ProposalValidationError,
        match="old-side content does not match current source",
    ):
        execute_bundle(
            repository,
            inspection,
            plan,
            bundle,
            registry,
            FakeBundleModelClient(invalid),
        )


def test_execution_retries_source_mismatch_once_with_current_source(
    bundle_fixture,
) -> None:
    repository, inspection, plan, bundle, registry = bundle_fixture
    valid = proposal(bundle.bundle_id)
    invalid_patch = valid.patches[0].patch.replace(
        " func Routes(r *gin.Engine) {\n"
        '-    r.GET("/ping", Ping)\n',
        '-    r.GET("/ping", Ping)\n'
        " func Routes(r *gin.Engine) {\n",
    )
    invalid = valid.model_copy(
        update={
            "patches": [
                BundleFilePatch(file="router.go", patch=invalid_patch)
            ]
        }
    )

    class CorrectingModel:
        model_name = "correcting-model"

        def __init__(self) -> None:
            self.calls = []

        def generate_bundle_patch(self, context, prompt):
            self.calls.append((context, prompt))
            return invalid if len(self.calls) == 1 else valid

    model = CorrectingModel()
    result = execute_bundle(
        repository,
        inspection,
        plan,
        bundle,
        registry,
        model,
    )

    assert result.proposal == valid
    assert len(model.calls) == 2
    assert "CORRECTION REQUIRED" in model.calls[1][1]
    assert '"router.go"' in model.calls[1][1]


def test_context_free_hunk_is_rejected(bundle_fixture) -> None:
    _, _, _, bundle, _ = bundle_fixture
    generated = proposal(bundle.bundle_id)
    patch = (
        "diff --git a/router.go b/router.go\n"
        "--- a/router.go\n+++ b/router.go\n"
        "@@ -1,1 +1,1 @@\n-old\n+new\n"
    )
    invalid = generated.model_copy(
        update={"patches": [BundleFilePatch(file="router.go", patch=patch)]}
    )

    with pytest.raises(ProposalValidationError, match="unchanged context"):
        validate_bundle_proposal(invalid, bundle)


def test_bundle_with_pending_predecessor_is_not_ready(bundle_fixture) -> None:
    _, _, plan, bundle, _ = bundle_fixture
    pending = bundle.model_copy(update={"depends_on": ["earlier-bundle"]})
    changed_plan = plan.model_copy(update={"bundles": [pending]})

    with pytest.raises(TaskSelectionError, match="pending bundles"):
        select_ready_bundle(changed_plan, pending.bundle_id)

    assert (
        select_ready_bundle(
            changed_plan,
            pending.bundle_id,
            completed_bundles=["earlier-bundle"],
        )
        == pending
    )


def test_gemini_is_the_default_bundle_provider(monkeypatch) -> None:
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("PATCH_MODEL_PROVIDER", "gemini")
    monkeypatch.delenv("PATCH_MODEL_NAME", raising=False)
    monkeypatch.setattr("llm.factory.load_project_environment", lambda: None)

    client = create_bundle_model_client()

    assert isinstance(client, GeminiModelClient)
    assert client.model_name == DEFAULT_GEMINI_MODEL


def test_bundle_model_adapter_validates_structured_json(bundle_fixture) -> None:
    _, _, _, bundle, _ = bundle_fixture
    expected = proposal(bundle.bundle_id)
    create = SimpleNamespace(
        create=lambda **_: SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=expected.model_dump_json(),
                        refusal=None,
                    )
                )
            ]
        )
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=create))
    adapter = AiHubMixModelClient(client=client)

    actual = adapter.generate_bundle_patch(None, "prompt")

    assert actual == expected


def test_bundle_model_adapter_rejects_invalid_json() -> None:
    create = SimpleNamespace(
        create=lambda **_: SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content="not-json", refusal=None)
                )
            ]
        )
    )
    client = SimpleNamespace(chat=SimpleNamespace(completions=create))
    adapter = AiHubMixModelClient(client=client)

    with pytest.raises(ModelInvocationError, match="invalid BundlePatchProposal"):
        adapter.generate_bundle_patch(None, "prompt")


def test_bundle_execute_cli_writes_validated_proposal(bundle_fixture, tmp_path) -> None:
    repository, inspection, plan, bundle, _ = bundle_fixture
    output = tmp_path / "bundle-proposal.json"
    stdout = io.StringIO()
    model = FakeBundleModelClient(proposal(bundle.bundle_id))

    with (
        patch("agent.bundle_cli.inspect_repository", return_value=inspection),
        patch(
            "agent.bundle_cli.build_repository_migration_plan",
            return_value=plan,
        ),
        patch(
            "agent.bundle_cli.create_bundle_model_client",
            return_value=model,
        ),
    ):
        exit_code = main(
            [
                "bundle",
                "execute",
                "--repo",
                str(repository),
                "--bundle",
                bundle.bundle_id,
                "--output",
                str(output),
            ],
            stdout=stdout,
            stderr=io.StringIO(),
        )

    assert exit_code == 0
    assert json.loads(output.read_text(encoding="utf-8"))["bundle_id"] == (
        bundle.bundle_id
    )
    assert "Validated patches: 1" in stdout.getvalue()


def test_bundle_execute_cli_does_not_call_model_for_unready_bundle(
    bundle_fixture,
    tmp_path,
) -> None:
    repository, inspection, plan, bundle, _ = bundle_fixture
    pending = bundle.model_copy(update={"depends_on": ["earlier-bundle"]})
    changed_plan = plan.model_copy(update={"bundles": [pending]})
    stderr = io.StringIO()

    with (
        patch("agent.bundle_cli.inspect_repository", return_value=inspection),
        patch(
            "agent.bundle_cli.build_repository_migration_plan",
            return_value=changed_plan,
        ),
        patch("agent.bundle_cli.create_bundle_model_client") as create_model,
    ):
        exit_code = main(
            [
                "bundle",
                "execute",
                "--repo",
                str(repository),
                "--bundle",
                pending.bundle_id,
                "--output",
                str(tmp_path / "proposal.json"),
            ],
            stdout=io.StringIO(),
            stderr=stderr,
        )

    assert exit_code == 1
    assert "not ready" in stderr.getvalue()
    create_model.assert_not_called()
