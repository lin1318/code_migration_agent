from __future__ import annotations

from pathlib import Path

import pytest

from agent.execution.errors import (
    ProposalValidationError,
    SuspiciousNoChangeProposalError,
)
from agent.execution.models import RelevantSourceFile
from agent.file_bundle_execution import (
    build_file_bundle_context,
    build_file_bundle_prompt,
    execute_file_bundle,
    validate_file_patch_proposal,
)
from knowledge.registry import load_rule_registry
from llm.fake import FakeFileBundleModelClient
from models.analyzer import AnalyzerResult, GinUsage
from models.file_bundle_execution import (
    FilePatchProposal,
    FilePatchStatus,
)
from models.file_bundles import FileMigrationBundle
from models.file_migration_plan import FileMigrationStage
from models.inspection import RepositoryInspection
from models.patch_proposal import PatchConfidence
from tools.inspection import DEFAULT_RULE_PATH
from tools.migration_planner import build_migration_inventory

GIN = "github.com/gin-gonic/gin"
SOURCE = '''package handler

import "github.com/gin-gonic/gin"

func Get(c *gin.Context) {
	_ = c.Query("id")
}
'''
UPDATED = '''package handler

import hertzApp "github.com/cloudwego/hertz/pkg/app"

func Get(c *hertzApp.RequestContext) {
	_ = c.Query("id")
}
'''


def inputs(tmp_path: Path):
    source_path = tmp_path / "handler" / "user.go"
    source_path.parent.mkdir()
    source_path.write_text(SOURCE, encoding="utf-8")
    analysis = AnalyzerResult(
        module_path="example.com/app",
        packages=[],
        usages=[
            GinUsage(
                file="handler/user.go",
                line=6,
                column=6,
                kind="context_method",
                symbol=f"{GIN}.(*Context).Query",
            )
        ],
        loading_errors=[],
    )
    registry = load_rule_registry(DEFAULT_RULE_PATH)
    inventory = build_migration_inventory(analysis, registry)
    inspection = RepositoryInspection.from_inventory(
        str(tmp_path), [], inventory
    )
    usage = inspection.discovered_gin_usages[0]
    bundle = FileMigrationBundle(
        bundle_id="file-bundle-handler-user",
        order=1,
        stage=FileMigrationStage.HANDLERS,
        file="handler/user.go",
        usage_ids=[usage.usage_id],
        rule_ids=["gin.context.query"],
        framework_symbols=[usage.symbol],
        read_only_context=[],
        review_required=False,
        reasons=["handler migration"],
    )
    context = build_file_bundle_context(
        tmp_path, inspection, bundle, registry
    )
    return inspection, registry, bundle, context


def proposal(bundle: FileMigrationBundle, content: str | None = UPDATED):
    return FilePatchProposal(
        bundle_id=bundle.bundle_id,
        file=bundle.file,
        status=(
            FilePatchStatus.CONTENT_GENERATED
            if content is not None
            else FilePatchStatus.NO_CHANGE_NEEDED
        ),
        summary="migrate handler",
        updated_content=content,
        confidence=PatchConfidence.HIGH,
        assumptions=[],
        manual_review_notes=[],
        unresolved_dependencies=[],
    )


def test_context_exposes_one_editable_file_and_owned_metadata(
    tmp_path: Path,
) -> None:
    _, _, bundle, context = inputs(tmp_path)

    assert context.editable_file.path == bundle.file
    assert context.editable_file.content.replace("\r\n", "\n") == SOURCE
    assert [usage.usage_id for usage in context.usages] == bundle.usage_ids
    assert [rule.rule_id for rule in context.migration_rules] == bundle.rule_ids


def test_prompt_marks_cross_file_information_read_only(tmp_path: Path) -> None:
    _, _, _, context = inputs(tmp_path)

    prompt = build_file_bundle_prompt(context)

    assert '"path": "handler/user.go"' in prompt
    assert "read-only" in prompt.lower()
    assert "Only `handler/user.go` may be edited" in prompt


def test_valid_complete_file_content_is_accepted(tmp_path: Path) -> None:
    _, _, bundle, context = inputs(tmp_path)

    validated = validate_file_patch_proposal(proposal(bundle), context)

    assert validated.updated_content == UPDATED


def test_proposal_for_another_file_is_rejected(tmp_path: Path) -> None:
    _, _, bundle, context = inputs(tmp_path)
    invalid = proposal(bundle).model_copy(update={"file": "handler/other.go"})

    with pytest.raises(ProposalValidationError):
        validate_file_patch_proposal(invalid, context)


def test_markdown_wrapped_content_is_rejected(tmp_path: Path) -> None:
    _, _, bundle, context = inputs(tmp_path)

    with pytest.raises(ProposalValidationError, match="Markdown"):
        validate_file_patch_proposal(
            proposal(bundle, f"```go\n{UPDATED}```"), context
        )


def test_no_change_is_rejected_while_owned_gin_usage_remains(
    tmp_path: Path,
) -> None:
    _, _, bundle, context = inputs(tmp_path)

    with pytest.raises(SuspiciousNoChangeProposalError):
        validate_file_patch_proposal(proposal(bundle, None), context)


def test_genuinely_unchanged_file_may_return_no_change(
    tmp_path: Path,
) -> None:
    _, _, bundle, context = inputs(tmp_path)
    unchanged = context.model_copy(
        update={
            "editable_file": RelevantSourceFile(
                path=bundle.file,
                content="package handler\n",
            ),
            "usages": [],
            "migration_rules": [],
        }
    )

    validate_file_patch_proposal(proposal(bundle, None), unchanged)


def test_execution_calls_model_without_modifying_repository(
    tmp_path: Path,
) -> None:
    inspection, registry, bundle, _ = inputs(tmp_path)
    client = FakeFileBundleModelClient(proposal(bundle))

    result = execute_file_bundle(
        tmp_path, inspection, bundle, registry, client
    )

    assert result.proposal.updated_content == UPDATED
    assert len(result.original_sha256) == 64
    assert result.file == bundle.file
    assert len(client.calls) == 1
    assert (tmp_path / bundle.file).read_text(encoding="utf-8") == SOURCE
