"""Typed single-file migration bundles and their deterministic ordering."""

from pydantic import Field

from models.file_migration_plan import FileMigrationStage
from models.migration_plan import PlanModel


class ReadOnlySymbolContext(PlanModel):
    symbol: str
    file: str
    line: int | None = Field(default=None, ge=1)
    signature: str
    framework_bound: bool


class FileMigrationBundle(PlanModel):
    bundle_id: str
    order: int = Field(ge=1)
    stage: FileMigrationStage
    file: str
    usage_ids: list[str]
    rule_ids: list[str]
    framework_symbols: list[str]
    read_only_context: list[ReadOnlySymbolContext]
    review_required: bool
    reasons: list[str] = Field(min_length=1)


class FileBundlePlan(PlanModel):
    file_count: int = Field(ge=0)
    usage_count: int = Field(ge=0)
    review_bundle_count: int = Field(ge=0)
    bundles: list[FileMigrationBundle]
    bundle_ordering: list[str]

