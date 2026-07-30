from pathlib import Path

import pytest

from knowledge.registry import (
    DuplicateRuleError,
    RuleFileError,
    load_rule_registry,
)
from models.migration_rules import MigrationConfidence


VALID_RULE = """
rules:
  - rule_id: gin.context.json
    source_package: github.com/gin-gonic/gin
    source_symbol: (*Context).JSON
    target_package: github.com/cloudwego/hertz/pkg/app
    target_symbol: (*RequestContext).JSON
    migration_category: response_rendering
    confidence: exact
    required_imports:
      - github.com/cloudwego/hertz/pkg/app
    notes: Preserve status and response arguments.
"""


def write_rules(directory: Path, name: str, content: str) -> Path:
    path = directory / name
    path.write_text(content, encoding="utf-8")
    return path


def test_loads_production_rules_and_performs_exact_lookup() -> None:
    knowledge = Path(__file__).resolve().parents[1] / "knowledge"

    registry = load_rule_registry(knowledge)

    assert len(registry) == 11
    rule = registry.lookup("github.com/gin-gonic/gin", "(*Context).JSON")
    assert rule is not None
    assert rule.rule_id == "gin.context.json"
    assert rule.confidence is MigrationConfidence.EXACT
    assert (
        registry.lookup_resolved_symbol(
            "github.com/gin-gonic/gin.(*Context).JSON"
        )
        == rule
    )


def test_lookup_requires_exact_package_and_symbol(tmp_path: Path) -> None:
    rule_file = write_rules(tmp_path, "valid.yaml", VALID_RULE)
    registry = load_rule_registry(rule_file)

    assert registry.lookup("github.com/gin-gonic/gin", "(*Context).JSON") is not None
    assert registry.lookup("github.com/gin-gonic/gin", "Context.JSON") is None
    assert registry.lookup("example.com/gin", "(*Context).JSON") is None


@pytest.mark.parametrize(
    "invalid_rule",
    [
        VALID_RULE.replace("confidence: exact", "confidence: guessed"),
        VALID_RULE.replace("    target_symbol: (*RequestContext).JSON\n", ""),
        VALID_RULE.replace(
            "    migration_category: response_rendering\n",
            "    target_operations:\n"
            "      - operation: call\n"
            "        symbol: (*RequestContext).JSON\n"
            "    migration_category: response_rendering\n",
        ),
        VALID_RULE.replace(
            "    notes: Preserve status and response arguments.\n",
            "    notes: Preserve status and response arguments.\n"
            "    unexpected: true\n",
        ),
    ],
)
def test_rejects_invalid_rule_schema(tmp_path: Path, invalid_rule: str) -> None:
    rule_file = write_rules(tmp_path, "invalid.yaml", invalid_rule)

    with pytest.raises(RuleFileError, match="invalid rule file"):
        load_rule_registry(rule_file)


def test_rejects_malformed_yaml(tmp_path: Path) -> None:
    rule_file = write_rules(tmp_path, "broken.yaml", "rules: [\n")

    with pytest.raises(RuleFileError, match="cannot load rule file"):
        load_rule_registry(rule_file)


def test_detects_duplicate_rule_ids_across_files(tmp_path: Path) -> None:
    write_rules(tmp_path, "first.yaml", VALID_RULE)
    duplicate = VALID_RULE.replace(
        "source_symbol: (*Context).JSON", "source_symbol: (*Context).Param"
    )
    write_rules(tmp_path, "second.yaml", duplicate)

    with pytest.raises(DuplicateRuleError, match="duplicate rule ID"):
        load_rule_registry(tmp_path)


def test_detects_duplicate_source_symbols(tmp_path: Path) -> None:
    duplicate = VALID_RULE.replace("rule_id: gin.context.json", "rule_id: duplicate")
    duplicate_item = duplicate.replace("\nrules:\n", "\n", 1)
    write_rules(tmp_path, "duplicates.yaml", VALID_RULE + duplicate_item)

    with pytest.raises(DuplicateRuleError, match="duplicate source symbol"):
        load_rule_registry(tmp_path)


def test_rejects_directory_without_yaml(tmp_path: Path) -> None:
    with pytest.raises(RuleFileError, match="no YAML rule files"):
        load_rule_registry(tmp_path)
