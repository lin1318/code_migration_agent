"""Validated data models for the Go analyzer JSON protocol."""

from pydantic import BaseModel, ConfigDict, Field


class AnalyzerModel(BaseModel):
    """Base model for strict validation of the analyzer protocol."""

    model_config = ConfigDict(extra="forbid")


class PackageLoadingError(AnalyzerModel):
    kind: str
    package: str | None = None
    position: str | None = None
    message: str


class AnalyzedPackage(AnalyzerModel):
    import_path: str
    name: str
    source_files: list[str]
    imports: list[str]


class GinUsage(AnalyzerModel):
    file: str
    line: int = Field(ge=1)
    column: int = Field(ge=1)
    kind: str
    symbol: str


class AnalyzerResult(AnalyzerModel):
    module_path: str
    packages: list[AnalyzedPackage]
    usages: list[GinUsage]
    loading_errors: list[PackageLoadingError]
