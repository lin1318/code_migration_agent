"""Build the minimum read-only repository context for one routing task."""

from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from agent.execution.errors import ContextBuildError, TaskSelectionError
from agent.execution.models import (
    MigrationTaskContext,
    NumberedSourceLine,
    PendingDependency,
    RelatedSymbolDefinition,
    RelevantLineRange,
    RelevantSourceFile,
)
from knowledge.registry import RuleRegistry
from models.inspection import RepositoryInspection
from models.migration_plan import InventoryUsage, MigrationPhase, MigrationTask

MAX_SOURCE_FILE_SIZE = 1024 * 1024
CONTEXT_LINE_RADIUS = 2


@dataclass(frozen=True)
class _HandlerReference:
    qualifier: str | None
    name: str
    source_path: str


def stable_task_id(task: MigrationTask) -> str:
    return f"migration-task-{task.order:02d}-{task.phase.value}"


def select_task(
    inspection: RepositoryInspection,
    *,
    phase: str,
    task_order: int | None = None,
) -> MigrationTask:
    if phase != MigrationPhase.ROUTING.value:
        raise TaskSelectionError(
            "select_task",
            f"Stage 8 supports only the routing phase, received: {phase}",
        )
    matches = [
        task
        for task in inspection.ordered_migration_tasks
        if task.phase == MigrationPhase.ROUTING
        and (task_order is None or task.order == task_order)
    ]
    if len(matches) != 1:
        raise TaskSelectionError(
            "select_task",
            f"expected exactly one routing task, found {len(matches)}",
            diagnostics={"task_order": task_order},
        )
    task = matches[0]
    if not task.direct_usage_ids:
        raise TaskSelectionError(
            "select_task",
            "the selected routing task has no direct usages",
            diagnostics={"task_order": task.order},
        )
    return task


def build_task_context(
    repository: str | os.PathLike[str],
    inspection: RepositoryInspection,
    task: MigrationTask,
    registry: RuleRegistry,
    *,
    completed_phases: list[str] | None = None,
    updated_source_content: dict[str, str] | None = None,
) -> MigrationTaskContext:
    root = _repository_root(repository)
    completed = completed_phases or []
    usages_by_id = {
        usage.usage_id: usage for usage in inspection.discovered_gin_usages
    }
    missing_usage_ids = [
        usage_id
        for usage_id in task.direct_usage_ids
        if usage_id not in usages_by_id
    ]
    if missing_usage_ids:
        raise ContextBuildError(
            "resolve_direct_usages",
            f"task references unknown usage IDs: {missing_usage_ids}",
        )
    direct_usages = [usages_by_id[usage_id] for usage_id in task.direct_usage_ids]
    allowed_files = sorted({usage.file for usage in direct_usages})
    overrides = updated_source_content or {}
    unexpected_overrides = sorted(set(overrides) - set(allowed_files))
    if unexpected_overrides:
        raise ContextBuildError(
            "validate_updated_source",
            "updated source content includes files outside allowed_files: "
            f"{unexpected_overrides}",
        )
    file_contents = {
        path: (
            _validate_updated_source(path, overrides[path])
            if path in overrides
            else _read_source_file(root, path)
        )
        for path in allowed_files
    }
    source_files = [
        RelevantSourceFile(path=path, content=file_contents[path])
        for path in allowed_files
    ]
    line_ranges = _build_line_ranges(direct_usages, file_contents)
    rules_by_id = {rule.rule_id: rule for rule in registry.rules}
    missing_rule_ids = [
        rule_id for rule_id in task.rule_ids if rule_id not in rules_by_id
    ]
    if missing_rule_ids:
        raise ContextBuildError(
            "resolve_migration_rules",
            f"task references unknown migration rule IDs: {missing_rule_ids}",
        )
    matching_rules = [rules_by_id[rule_id] for rule_id in task.rule_ids]
    handler_symbols = _routing_handler_symbols(direct_usages, file_contents)
    definitions = _find_symbol_definitions(
        root,
        inspection.repository_summary.module_path,
        handler_symbols,
        file_contents,
    )
    dependencies = [
        PendingDependency(
            phase=planned.phase.value,
            task_order=planned.order,
            description=planned.description,
        )
        for planned in inspection.ordered_migration_tasks
        if planned.order < task.order and planned.phase.value not in completed
    ]
    return MigrationTaskContext(
        repository_summary=inspection.repository_summary,
        migration_task_id=stable_task_id(task),
        selected_migration_task=task,
        allowed_files=allowed_files,
        direct_usages=direct_usages,
        matching_migration_rules=matching_rules,
        relevant_source_files=source_files,
        explicitly_updated_source_files=sorted(overrides),
        relevant_line_ranges=line_ranges,
        related_symbol_definitions=definitions,
        completed_migration_phases=completed,
        pending_dependencies=dependencies,
    )


def _validate_updated_source(path: str, content: str) -> str:
    _safe_relative_path(path)
    if "\x00" in content:
        raise ContextBuildError(
            "validate_updated_source",
            f"binary updated source content is not allowed: {path}",
        )
    if len(content.encode("utf-8")) > MAX_SOURCE_FILE_SIZE:
        raise ContextBuildError(
            "validate_updated_source",
            f"updated source content exceeds {MAX_SOURCE_FILE_SIZE} bytes: {path}",
        )
    return content


def _repository_root(repository: str | os.PathLike[str]) -> Path:
    root = Path(repository).expanduser()
    if not root.exists():
        raise ContextBuildError(
            "validate_repository",
            f"repository path does not exist: {root}",
        )
    if not root.is_dir():
        raise ContextBuildError(
            "validate_repository",
            f"repository path is not a directory: {root}",
        )
    return root.resolve()


def _safe_relative_path(value: str) -> PurePosixPath:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or ".." in path.parts
        or any(part.casefold() == ".git" for part in path.parts)
        or (path.parts and path.parts[0].endswith(":"))
    ):
        raise ContextBuildError(
            "validate_source_path",
            f"unsafe repository-relative source path: {value}",
        )
    return path


def _read_source_file(root: Path, relative_path: str) -> str:
    safe_path = _safe_relative_path(relative_path)
    target = root.joinpath(*safe_path.parts).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError as error:
        raise ContextBuildError(
            "read_source_file",
            f"source path escapes repository: {relative_path}",
        ) from error
    if not target.is_file():
        raise ContextBuildError(
            "read_source_file",
            f"source file does not exist: {relative_path}",
        )
    size = target.stat().st_size
    if size > MAX_SOURCE_FILE_SIZE:
        raise ContextBuildError(
            "read_source_file",
            f"source file exceeds {MAX_SOURCE_FILE_SIZE} bytes: {relative_path}",
        )
    data = target.read_bytes()
    if b"\x00" in data:
        raise ContextBuildError(
            "read_source_file",
            f"binary source file is not allowed: {relative_path}",
        )
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ContextBuildError(
            "read_source_file",
            f"source file is not valid UTF-8: {relative_path}",
        ) from error


def _build_line_ranges(
    usages: list[InventoryUsage],
    contents: dict[str, str],
) -> list[RelevantLineRange]:
    intervals: dict[str, list[tuple[int, int]]] = defaultdict(list)
    lines_by_file = {
        path: content.splitlines() for path, content in contents.items()
    }
    for usage in usages:
        line_count = len(lines_by_file[usage.file])
        if usage.line > line_count:
            raise ContextBuildError(
                "build_line_ranges",
                f"usage line {usage.line} is outside {usage.file}",
            )
        intervals[usage.file].append(
            (
                max(1, usage.line - CONTEXT_LINE_RADIUS),
                min(line_count, usage.line + CONTEXT_LINE_RADIUS),
            )
        )
    ranges: list[RelevantLineRange] = []
    for path in sorted(intervals):
        merged: list[list[int]] = []
        for start, end in sorted(intervals[path]):
            if merged and start <= merged[-1][1] + 1:
                merged[-1][1] = max(merged[-1][1], end)
            else:
                merged.append([start, end])
        file_lines = lines_by_file[path]
        for start, end in merged:
            ranges.append(
                RelevantLineRange(
                    path=path,
                    start_line=start,
                    end_line=end,
                    lines=[
                        NumberedSourceLine(number=line, text=file_lines[line - 1])
                        for line in range(start, end + 1)
                    ],
                )
            )
    return ranges


def _routing_handler_symbols(
    usages: list[InventoryUsage],
    contents: dict[str, str],
) -> set[_HandlerReference]:
    handlers: set[_HandlerReference] = set()
    for usage in usages:
        lines = contents[usage.file].splitlines()
        source_line = lines[usage.line - 1].strip()
        opening = source_line.find("(")
        closing = source_line.rfind(")")
        if opening < 0 or closing <= opening:
            continue
        arguments = _split_call_arguments(source_line[opening + 1 : closing])
        for argument in arguments[1:]:
            expression = argument.strip()
            if not expression:
                continue
            call = expression.find("(")
            if call >= 0:
                expression = expression[:call]
            components = [part.strip() for part in expression.rsplit(".", 1)]
            identifier = components[-1]
            if identifier and identifier.replace("_", "").isalnum():
                qualifier = components[0] if len(components) == 2 else None
                handlers.add(
                    _HandlerReference(
                        qualifier=qualifier,
                        name=identifier,
                        source_path=usage.file,
                    )
                )
    return handlers


def _split_call_arguments(arguments: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    quote: str | None = None
    escaped = False
    for index, character in enumerate(arguments):
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in ('"', "'", "`"):
            quote = character
        elif character in "([{":
            depth += 1
        elif character in ")]}":
            depth = max(0, depth - 1)
        elif character == "," and depth == 0:
            parts.append(arguments[start:index])
            start = index + 1
    parts.append(arguments[start:])
    return parts


def _find_symbol_definitions(
    root: Path,
    module_path: str,
    references: set[_HandlerReference],
    source_contents: dict[str, str],
) -> list[RelatedSymbolDefinition]:
    if not references:
        return []
    definitions: list[RelatedSymbolDefinition] = []
    for reference in sorted(
        references,
        key=lambda item: (item.source_path, item.qualifier or "", item.name),
    ):
        source_path = PurePosixPath(reference.source_path)
        if reference.qualifier is None:
            package_directory = root.joinpath(*source_path.parent.parts)
        else:
            imports = _import_aliases(source_contents[reference.source_path])
            import_path = imports.get(reference.qualifier)
            if import_path is None or not import_path.startswith(f"{module_path}/"):
                continue
            relative_package = import_path.removeprefix(f"{module_path}/")
            package_directory = root.joinpath(
                *PurePosixPath(relative_package).parts
            )
        if not package_directory.is_dir():
            continue
        for path in sorted(package_directory.glob("*.go")):
            relative = path.relative_to(root)
            if path.is_symlink() or path.stat().st_size > MAX_SOURCE_FILE_SIZE:
                continue
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                continue
            for index, line in enumerate(lines):
                stripped = line.strip()
                if not stripped.startswith("func "):
                    continue
                signature = stripped
                cursor = index + 1
                while not _has_function_body(signature) and cursor < min(
                    len(lines),
                    index + 10,
                ):
                    signature = f"{signature} {lines[cursor].strip()}"
                    cursor += 1
                name = _function_name(signature)
                if name == reference.name:
                    definitions.append(
                        RelatedSymbolDefinition(
                            symbol=(
                                f"{reference.qualifier}.{name}"
                                if reference.qualifier
                                else name
                            ),
                            path=relative.as_posix(),
                            line=index + 1,
                            signature=_signature_without_body(signature),
                        )
                    )
    return definitions


def _import_aliases(content: str) -> dict[str, str]:
    aliases: dict[str, str] = {}
    in_block = False
    for line in content.splitlines():
        stripped = line.strip()
        if stripped == "import (":
            in_block = True
            continue
        if in_block and stripped == ")":
            in_block = False
            continue
        if not in_block and not stripped.startswith("import "):
            continue
        declaration = stripped if in_block else stripped.removeprefix("import ").strip()
        first_quote = declaration.find('"')
        last_quote = declaration.rfind('"')
        if first_quote < 0 or last_quote <= first_quote:
            continue
        import_path = declaration[first_quote + 1 : last_quote]
        explicit_alias = declaration[:first_quote].strip()
        alias = explicit_alias or import_path.rsplit("/", 1)[-1]
        if alias not in {"_", "."}:
            aliases[alias] = import_path
    return aliases


def _has_function_body(signature: str) -> bool:
    return signature.rstrip().endswith("{") or ") {" in signature


def _signature_without_body(signature: str) -> str:
    body_marker = signature.rfind(") {")
    if body_marker >= 0:
        return signature[: body_marker + 1].rstrip()
    if signature.rstrip().endswith("{"):
        return signature.rstrip()[:-1].rstrip()
    return signature


def _function_name(signature: str) -> str | None:
    remainder = signature.removeprefix("func ").lstrip()
    if remainder.startswith("("):
        depth = 0
        receiver_end = None
        for index, character in enumerate(remainder):
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0:
                    receiver_end = index + 1
                    break
        if receiver_end is None:
            return None
        remainder = remainder[receiver_end:].lstrip()
    name = remainder.split("(", 1)[0].strip()
    return name if name.replace("_", "").isalnum() else None
