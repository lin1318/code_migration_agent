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


class SymbolReference(AnalyzerModel):
    file: str
    line: int = Field(ge=1)
    column: int = Field(ge=1)
    enclosing_symbol: str = ""
    symbol: str
    package_path: str = ""
    module_path: str = ""
    definition_file: str = ""
    definition_line: int = Field(default=0, ge=0)
    signature: str = ""
    framework_bound: bool = False
    standard_library: bool = False
    resolved: bool = False
    receiver_root_type: str = ""
    receiver_path: list[str] = Field(default_factory=list)


class AnalyzerResult(AnalyzerModel):
    module_path: str
    packages: list[AnalyzedPackage]
    usages: list[GinUsage]
    references: list[SymbolReference] = Field(default_factory=list)
    loading_errors: list[PackageLoadingError]
