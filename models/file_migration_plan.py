"""Typed deterministic plan for uniquely owned Gin migration files."""

from enum import StrEnum

from pydantic import Field

from models.migration_plan import PlanModel


class FileMigrationStage(StrEnum):
    SHARED_CONTEXT = "shared_context"
    HANDLERS = "handlers"
    MIDDLEWARE = "middleware"
    ROUTING = "routing"
    BOOTSTRAP = "bootstrap"


class FileMigrationStagePlan(PlanModel):
    order: int = Field(ge=1, le=5)
    stage: FileMigrationStage
    files: list[str]
    usage_ids: list[str]
    symbols: list[str]
    review_required: bool
    review_files: list[str]


class FileMigrationPlan(PlanModel):
    file_count: int = Field(ge=0)
    usage_count: int = Field(ge=0)
    review_file_count: int = Field(ge=0)
    stages: list[FileMigrationStagePlan] = Field(min_length=5, max_length=5)

