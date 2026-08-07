"""Typed results for deterministic Gin file-role classification."""

from enum import StrEnum

from pydantic import Field

from models.migration_plan import PlanModel


class GinFileRole(StrEnum):
    SHARED_CONTEXT = "shared_context"
    HANDLER = "handler"
    MIDDLEWARE = "middleware"
    ROUTING = "routing"
    BOOTSTRAP = "bootstrap"


class FileRoleConfidence(StrEnum):
    EXACT = "exact"
    REVIEW = "review"


class ClassifiedGinFile(PlanModel):
    file: str
    role: GinFileRole
    confidence: FileRoleConfidence
    usage_ids: list[str]
    symbols: list[str]
    candidate_roles: list[GinFileRole]
    reasons: list[str] = Field(min_length=1)


class GinFileClassification(PlanModel):
    files: list[ClassifiedGinFile]
