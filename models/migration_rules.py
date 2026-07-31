"""Typed schema for declarative Gin-to-Hertz migration rules."""

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RuleModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MigrationConfidence(StrEnum):
    EXACT = "exact"
    REVIEW = "review"
    UNSUPPORTED = "unsupported"


class RuleDependencyKind(StrEnum):
    FRAMEWORK_API = "framework_api"
    EXTERNAL_DEPENDENCY = "external_dependency"


class ResolutionStrategy(StrEnum):
    MIGRATE_SYMBOL = "migrate_symbol"
    REPLACE_DEPENDENCY = "replace_dependency"


class MigrationCategory(StrEnum):
    SERVER_INITIALIZATION = "server_initialization"
    ROUTING = "routing"
    CONTEXT_TYPE = "context_type"
    HANDLER_TYPE = "handler_type"
    REQUEST_ACCESS = "request_access"
    RESPONSE_RENDERING = "response_rendering"
    DATA_BINDING = "data_binding"
    MIDDLEWARE_CONTROL = "middleware_control"
    DATA_STRUCTURE = "data_structure"


class TargetOperation(RuleModel):
    operation: str = Field(min_length=1)
    symbol: str | None = None
    value: str | None = None
    notes: str | None = None


class MigrationRule(RuleModel):
    rule_id: str = Field(min_length=1, pattern=r"^[a-z][a-z0-9_.-]*$")
    source_package: str = Field(min_length=1)
    source_symbol: str = Field(min_length=1)
    target_package: str = Field(min_length=1)
    target_symbol: str | None = None
    target_operations: list[TargetOperation] | None = None
    migration_category: MigrationCategory
    confidence: MigrationConfidence
    required_imports: list[str]
    notes: str = Field(min_length=1)
    dependency_kind: RuleDependencyKind = RuleDependencyKind.FRAMEWORK_API
    resolution_strategy: ResolutionStrategy = ResolutionStrategy.MIGRATE_SYMBOL
    documentation_notes: str | None = None

    @model_validator(mode="after")
    def require_exactly_one_target(self) -> "MigrationRule":
        has_symbol = self.target_symbol is not None
        has_operations = bool(self.target_operations)
        if has_symbol == has_operations:
            raise ValueError(
                "exactly one of target_symbol or target_operations must be provided"
            )
        return self

    @property
    def resolved_source_symbol(self) -> str:
        return f"{self.source_package}.{self.source_symbol}"


class MigrationRuleFile(RuleModel):
    rules: list[MigrationRule] = Field(min_length=1)
