"""Deterministic semantic classification of files affected by Gin migration."""

from __future__ import annotations

from collections import defaultdict
from pathlib import PurePosixPath

from models.file_classification import (
    ClassifiedGinFile,
    FileRoleConfidence,
    GinFileClassification,
    GinFileRole,
)
from models.inspection import RepositoryInspection

_GIN_PACKAGE = "github.com/gin-gonic/gin"
_ROUTE_METHODS = {"GET", "POST", "PUT", "DELETE", "Group"}
_MIDDLEWARE_METHODS = {"Next", "Abort", "AbortWithStatusJSON"}
_ROLE_ORDER = {
    GinFileRole.BOOTSTRAP: 0,
    GinFileRole.ROUTING: 1,
    GinFileRole.MIDDLEWARE: 2,
    GinFileRole.HANDLER: 3,
    GinFileRole.SHARED_CONTEXT: 4,
}


def classify_gin_files(
    inspection: RepositoryInspection,
) -> GinFileClassification:
    """Classify every file with a discovered Gin usage by semantic role."""

    usages_by_file = defaultdict(list)
    for usage in inspection.discovered_gin_usages:
        usages_by_file[_normalized(usage.file)].append(usage)

    references = inspection.semantic_references
    route_locations = {
        (_normalized(usage.file), usage.line)
        for usage in inspection.discovered_gin_usages
        if _is_route_usage(usage.symbol, usage.kind)
    }
    use_locations = {
        (_normalized(usage.file), usage.line)
        for usage in inspection.discovered_gin_usages
        if _symbol_name(usage.symbol) == "Use"
    }
    handler_symbols: set[str] = set()
    handler_files: dict[str, set[str]] = defaultdict(set)
    middleware_files: dict[str, set[str]] = defaultdict(set)
    module_path = inspection.repository_summary.module_path

    for reference in references:
        location = (_normalized(reference.file), reference.line)
        if not _is_local_definition(reference, module_path):
            continue
        definition_file = _normalized(reference.definition_file)
        if location in route_locations:
            handler_symbols.add(reference.symbol)
            handler_files[definition_file].add(reference.symbol)
        if location in use_locations:
            middleware_files[definition_file].add(reference.symbol)

    shared_files: dict[str, set[str]] = defaultdict(set)
    for reference in references:
        if (
            reference.enclosing_symbol in handler_symbols
            and reference.framework_bound
            and _is_local_definition(reference, module_path)
            and _normalized(reference.definition_file)
            != _normalized(reference.file)
        ):
            shared_files[_normalized(reference.definition_file)].add(
                reference.symbol
            )

    classified: list[ClassifiedGinFile] = []
    for file in sorted(usages_by_file):
        usages = usages_by_file[file]
        evidence: dict[GinFileRole, list[tuple[str, bool]]] = defaultdict(list)

        if any(
            _normalized(reference.file) == file
            and reference.enclosing_symbol.rsplit(".", 1)[-1] == "main"
            for reference in references
        ):
            evidence[GinFileRole.BOOTSTRAP].append(
                ("Gin usage occurs inside the package main function", True)
            )

        route_symbols = sorted(
            {
                usage.symbol
                for usage in usages
                if _is_route_usage(usage.symbol, usage.kind)
            }
        )
        if route_symbols:
            evidence[GinFileRole.ROUTING].append(
                ("contains semantic Gin route registration or grouping", True)
            )

        middleware_symbols = sorted(
            {
                usage.symbol
                for usage in usages
                if _is_middleware_body_usage(usage.symbol)
            }
        )
        if middleware_symbols:
            evidence[GinFileRole.MIDDLEWARE].append(
                ("contains Gin middleware handler or chain-control symbols", True)
            )
        if file in middleware_files:
            evidence[GinFileRole.MIDDLEWARE].append(
                ("a function defined in this file is registered with Gin Use", True)
            )

        if file in handler_files:
            evidence[GinFileRole.HANDLER].append(
                ("a function defined in this file is registered as a route handler", True)
            )

        if file in shared_files:
            evidence[GinFileRole.SHARED_CONTEXT].append(
                (
                    "a Gin-bound function in this file is referenced by a registered handler",
                    False,
                )
            )

        if _has_context_usage(usages) and not evidence:
            evidence[GinFileRole.HANDLER].append(
                ("contains Gin context operations but has no resolved route reference", False)
            )

        if not evidence:
            _add_path_hint(file, evidence)

        if not evidence:
            evidence[GinFileRole.SHARED_CONTEXT].append(
                (
                    "Gin-related file is not bootstrap, routing, middleware, or a "
                    "registered handler; classify it as a project framework wrapper",
                    False,
                )
            )

        candidates = sorted(evidence, key=_ROLE_ORDER.__getitem__)
        role = candidates[0]
        exact_roles = [
            candidate
            for candidate, items in evidence.items()
            if any(exact for _, exact in items)
        ]
        confidence = (
            FileRoleConfidence.EXACT
            if role in exact_roles and len(exact_roles) == 1
            else FileRoleConfidence.REVIEW
        )
        reasons = [
            reason
            for candidate in candidates
            for reason, _ in evidence[candidate]
        ]
        if len(exact_roles) > 1:
            reasons.append(
                "multiple semantic roles were detected; the primary role requires review"
            )

        classified.append(
            ClassifiedGinFile(
                file=usages[0].file.replace("\\", "/"),
                role=role,
                confidence=confidence,
                usage_ids=sorted(usage.usage_id for usage in usages),
                symbols=sorted({usage.symbol for usage in usages}),
                candidate_roles=candidates,
                reasons=reasons,
            )
        )

    return GinFileClassification(files=classified)


def _is_route_usage(symbol: str, kind: str) -> bool:
    return kind in {"engine_method", "router_group_method", "group"} or (
        _symbol_name(symbol) in _ROUTE_METHODS
        and (".(*Engine)." in symbol or ".(*RouterGroup)." in symbol)
    )


def _is_middleware_body_usage(symbol: str) -> bool:
    return symbol.endswith(".HandlerFunc") or (
        ".(*Context)." in symbol and _symbol_name(symbol) in _MIDDLEWARE_METHODS
    )


def _has_context_usage(usages) -> bool:
    return any(
        usage.kind in {"context_method", "gin_type", "gin_h"}
        or ".(*Context)." in usage.symbol
        or usage.symbol.endswith(".Context")
        for usage in usages
        if usage.kind != "gin_import"
    )


def _is_local_definition(reference, module_path: str) -> bool:
    return bool(
        reference.resolved
        and reference.definition_file
        and module_path
        and (
            reference.package_path == module_path
            or reference.package_path.startswith(f"{module_path}/")
        )
    )


def _symbol_name(symbol: str) -> str:
    return symbol.rsplit(".", 1)[-1]


def _normalized(path: str) -> str:
    return path.replace("\\", "/").casefold()


def _add_path_hint(
    file: str,
    evidence: dict[GinFileRole, list[tuple[str, bool]]],
) -> None:
    path = PurePosixPath(file)
    lowered = {part.casefold() for part in path.parts}
    stem = path.stem.casefold()
    if path.name.casefold() == "main.go":
        evidence[GinFileRole.BOOTSTRAP].append(
            ("main.go is a bootstrap naming hint without semantic confirmation", False)
        )
    elif lowered & {"middleware", "middlewares"}:
        evidence[GinFileRole.MIDDLEWARE].append(
            ("middleware directory is a naming hint without semantic confirmation", False)
        )
    elif lowered & {"router", "routers", "routes"} or stem in {"router", "routes"}:
        evidence[GinFileRole.ROUTING].append(
            ("router path is a naming hint without semantic confirmation", False)
        )
    elif lowered & {"handler", "handlers", "controller", "controllers"}:
        evidence[GinFileRole.HANDLER].append(
            ("handler path is a naming hint without semantic confirmation", False)
        )
    elif stem in {"context", "response", "form", "render"}:
        evidence[GinFileRole.SHARED_CONTEXT].append(
            ("shared helper filename is a naming hint without semantic confirmation", False)
        )
