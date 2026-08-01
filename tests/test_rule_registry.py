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

    assert len(registry) == 35
    rule = registry.lookup("github.com/gin-gonic/gin", "(*Context).JSON")
    assert rule is not None
    assert rule.rule_id == "gin.context.json"
    assert rule.confidence is MigrationConfidence.EXACT
    assert rule.target_import_alias == "hertzApp"
    assert (
        registry.lookup_resolved_symbol(
            "github.com/gin-gonic/gin.(*Context).JSON"
        )
        == rule
    )

    form_file = registry.lookup("net/http", "(*Request).FormFile")
    assert form_file is not None
    assert form_file.rule_id == "net-http.request.form-file-to-hertz"
    operations = [
        operation.operation
        for operation in form_file.target_operations or []
    ]
    assert operations == [
        "replace_receiver_call",
        "open_multipart_file",
        "close_multipart_file",
    ]

    set_mode = registry.lookup("github.com/gin-gonic/gin", "SetMode")
    assert set_mode is not None
    assert [item.operation for item in set_mode.target_operations or []] == [
        "delete_call"
    ]

    listen = registry.lookup("net/http", "(*Server).ListenAndServe")
    assert listen is not None
    assert listen.rule_id == "net-http.server.listen-and-serve-to-hertz"
    assert [item.operation for item in listen.target_operations or []] == [
        "remove_http_server_wrapper",
        "configure_hertz_constructor",
        "configure_read_timeout",
        "configure_write_timeout",
        "configure_max_header_bytes",
        "start_hertz",
    ]

    gin_new = registry.lookup("github.com/gin-gonic/gin", "New")
    assert gin_new is not None
    assert "github.com/cloudwego/hertz/pkg/common/config" in gin_new.required_imports
    assert "github.com/cloudwego/hertz/pkg/app/server/config" not in (
        gin_new.required_imports
    )

    swagger = registry.lookup("github.com/swaggo/gin-swagger", "WrapHandler")
    assert swagger is not None
    assert [item.operation for item in swagger.target_operations or []] == [
        "pass_handler_function_value",
        "wrap_http_handler",
    ]
    assert "without invoking" in (
        (swagger.target_operations or [])[0].notes or ""
    )
    assert (
        (swagger.target_operations or [])[1].value
        == "github.com/swaggo/http-swagger.WrapHandler"
    )


@pytest.mark.parametrize(
    "source_symbol",
    [
        "(*Engine).GET",
        "(*Engine).POST",
        "(*Engine).PUT",
        "(*Engine).DELETE",
        "(*Engine).Group",
        "(*Engine).Use",
        "(*RouterGroup).GET",
        "(*RouterGroup).POST",
        "(*RouterGroup).PUT",
        "(*RouterGroup).DELETE",
        "(*RouterGroup).Group",
        "(*RouterGroup).Use",
    ],
)
def test_registry_covers_analyzed_routing_symbols(source_symbol: str) -> None:
    knowledge = Path(__file__).resolve().parents[1] / "knowledge"
    registry = load_rule_registry(knowledge)

    assert registry.lookup("github.com/gin-gonic/gin", source_symbol) is not None


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
        VALID_RULE.replace(
            "    target_package: github.com/cloudwego/hertz/pkg/app\n",
            "    target_package: github.com/cloudwego/hertz/pkg/app\n"
            "    target_import_alias: 9invalid\n",
        ),
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
