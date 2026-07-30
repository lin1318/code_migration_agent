"""Loading and exact lookup for YAML migration rules."""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import yaml
from pydantic import ValidationError

from models.migration_rules import MigrationRule, MigrationRuleFile


class RuleRegistryError(RuntimeError):
    pass


class RuleFileError(RuleRegistryError):
    pass


class DuplicateRuleError(RuleRegistryError):
    pass


class RuleRegistry:
    """An immutable registry indexed by exact source symbols."""

    def __init__(self, rules: Iterable[MigrationRule]) -> None:
        by_id: dict[str, MigrationRule] = {}
        by_source: dict[tuple[str, str], MigrationRule] = {}
        for rule in rules:
            if previous := by_id.get(rule.rule_id):
                raise DuplicateRuleError(
                    f"duplicate rule ID {rule.rule_id!r}: "
                    f"{previous.resolved_source_symbol} and "
                    f"{rule.resolved_source_symbol}"
                )
            source_key = (rule.source_package, rule.source_symbol)
            if previous := by_source.get(source_key):
                raise DuplicateRuleError(
                    f"duplicate source symbol {rule.resolved_source_symbol!r}: "
                    f"rules {previous.rule_id!r} and {rule.rule_id!r}"
                )
            by_id[rule.rule_id] = rule
            by_source[source_key] = rule

        self._by_id = by_id
        self._by_source = by_source
        self._by_resolved = {
            rule.resolved_source_symbol: rule for rule in by_id.values()
        }

    def __len__(self) -> int:
        return len(self._by_id)

    @property
    def rules(self) -> tuple[MigrationRule, ...]:
        return tuple(self._by_id.values())

    def lookup(self, source_package: str, source_symbol: str) -> MigrationRule | None:
        """Return a rule only for an exact package and symbol match."""

        return self._by_source.get((source_package, source_symbol))

    def lookup_resolved_symbol(self, symbol: str) -> MigrationRule | None:
        """Look up the exact fully resolved symbol emitted by the Go analyzer."""

        return self._by_resolved.get(symbol)


def load_rule_registry(path: str | Path) -> RuleRegistry:
    """Load and validate all YAML rule files under path."""

    root = Path(path)
    if not root.exists():
        raise RuleFileError(f"rule path does not exist: {root}")
    files = (
        [root]
        if root.is_file()
        else sorted((*root.rglob("*.yaml"), *root.rglob("*.yml")))
    )
    if not files:
        raise RuleFileError(f"no YAML rule files found under: {root}")

    rules: list[MigrationRule] = []
    for file in files:
        try:
            with file.open(encoding="utf-8") as stream:
                document = yaml.safe_load(stream)
        except (OSError, yaml.YAMLError) as error:
            raise RuleFileError(f"cannot load rule file {file}: {error}") from error
        try:
            validated = MigrationRuleFile.model_validate(document)
        except ValidationError as error:
            raise RuleFileError(f"invalid rule file {file}: {error}") from error
        rules.extend(validated.rules)

    return RuleRegistry(rules)
