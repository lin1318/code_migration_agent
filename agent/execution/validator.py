"""Validate a structured proposal without applying its patch."""

from __future__ import annotations

import re
import shlex
from pathlib import PurePosixPath

from agent.execution.errors import (
    ProposalValidationError,
    SuspiciousNoChangeProposalError,
)
from agent.execution.models import MigrationTaskContext
from models.migration_rules import MigrationRule
from models.patch_proposal import PatchProposal, PatchProposalStatus

_HUNK_HEADER = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new>\d+))? @@(?P<suffix>.*)$"
)


def validate_proposal(
    proposal: PatchProposal,
    *,
    expected_task_id: str,
    allowed_files: set[str],
    context: MigrationTaskContext | None = None,
) -> PatchProposal:
    if proposal.task_id != expected_task_id:
        raise ProposalValidationError(
            "validate_proposal",
            f"proposal task_id {proposal.task_id!r} does not match "
            f"{expected_task_id!r}",
        )
    normalized_allowed = {_safe_path(path) for path in allowed_files}
    proposed_files = {_safe_path(path) for path in proposal.files_to_modify}
    if not proposed_files.issubset(normalized_allowed):
        outside = sorted(proposed_files - normalized_allowed)
        raise ProposalValidationError(
            "validate_proposal",
            f"proposal files are outside allowed_files: {outside}",
        )
    if proposal.patch is None:
        if proposed_files:
            raise ProposalValidationError(
                "validate_proposal",
                "a proposal without a patch must not list files_to_modify",
            )
        if (
            proposal.status == PatchProposalStatus.NO_CHANGE_NEEDED
            and context is not None
        ):
            _validate_no_change_needed(context)
        return proposal
    patch = proposal.patch
    if "```" in patch:
        raise ProposalValidationError(
            "validate_patch",
            "Markdown code fences are not allowed in patch output",
        )
    affected_files = _parse_patch_paths(patch)
    if affected_files != proposed_files:
        raise ProposalValidationError(
            "validate_patch",
            "files_to_modify must exactly match the files affected by the patch",
            diagnostics={
                "affected_files": ",".join(sorted(affected_files)),
                "files_to_modify": ",".join(sorted(proposed_files)),
            },
        )
    if not affected_files.issubset(normalized_allowed):
        outside = sorted(affected_files - normalized_allowed)
        raise ProposalValidationError(
            "validate_patch",
            f"patch modifies files outside allowed_files: {outside}",
        )
    return proposal


def validate_patch_document(
    patch: str,
    *,
    expected_file: str,
    allowed_files: set[str],
) -> str:
    """Validate one existing-file unified diff using Stage 8 safeguards."""

    normalized_expected = _safe_path(expected_file)
    normalized_allowed = {_safe_path(path) for path in allowed_files}
    if normalized_expected not in normalized_allowed:
        raise ProposalValidationError(
            "validate_patch",
            f"patch file is outside allowed_files: {normalized_expected}",
        )
    if "```" in patch:
        raise ProposalValidationError(
            "validate_patch",
            "Markdown code fences are not allowed in patch output",
        )
    affected = _parse_patch_paths(patch)
    if affected != {normalized_expected}:
        raise ProposalValidationError(
            "validate_patch",
            "each bundle patch must modify exactly its declared file",
        )
    return normalized_expected


def validate_patch_old_content(
    patch: str,
    *,
    source: str,
    expected_file: str,
) -> None:
    """Require every hunk old side to identify current source unambiguously."""

    source_lines = source.splitlines()
    patch_lines = patch.splitlines()
    index = 0
    while index < len(patch_lines):
        header = patch_lines[index]
        match = _HUNK_HEADER.match(header)
        if match is None:
            index += 1
            continue
        old_start = int(match.group("old_start"))
        old_count = int(match.group("old") or "1")
        old_lines: list[str] = []
        index += 1
        while index < len(patch_lines) and not patch_lines[index].startswith(
            ("@@ ", "diff --git ")
        ):
            body = patch_lines[index]
            if body.startswith("\\ No newline at end of file"):
                index += 1
                continue
            if body.startswith((" ", "-")):
                old_lines.append(body[1:])
            index += 1

        start_index = old_start - 1
        actual = source_lines[start_index : start_index + old_count]
        if start_index >= 0 and old_lines == actual:
            continue

        matching_starts = [
            candidate + 1
            for candidate in range(len(source_lines) - len(old_lines) + 1)
            if source_lines[candidate : candidate + len(old_lines)] == old_lines
        ]
        if len(matching_starts) != 1:
            raise ProposalValidationError(
                "validate_patch_source",
                f"patch old-side content does not match current source for "
                f"{expected_file!r} at hunk starting line {old_start}; "
                f"matching locations found: {matching_starts}",
                diagnostics={
                    "file": expected_file,
                    "hunk": header,
                    "old_start": str(old_start),
                    "matching_location_count": len(matching_starts),
                },
            )


def normalize_patch_hunk_counts(patch: str) -> str:
    """Correct only mechanical unified-diff hunk counts."""

    lines = patch.splitlines()
    changed = False
    index = 0
    while index < len(lines):
        match = _HUNK_HEADER.match(lines[index])
        if match is None:
            index += 1
            continue
        header_index = index
        expected_old = int(match.group("old") or "1")
        expected_new = int(match.group("new") or "1")
        actual_old = 0
        actual_new = 0
        index += 1
        while index < len(lines) and not lines[index].startswith(
            ("@@ ", "diff --git ")
        ):
            body = lines[index]
            if body.startswith(" "):
                actual_old += 1
                actual_new += 1
            elif body.startswith("-") and not body.startswith("--- "):
                actual_old += 1
            elif body.startswith("+") and not body.startswith("+++ "):
                actual_new += 1
            index += 1
        if (actual_old, actual_new) != (expected_old, expected_new):
            lines[header_index] = (
                f"@@ -{match.group('old_start')},{actual_old} "
                f"+{match.group('new_start')},{actual_new} @@"
                f"{match.group('suffix')}"
            )
            changed = True
    if not changed:
        return patch
    normalized = "\n".join(lines)
    return normalized + ("\n" if patch.endswith(("\n", "\r")) else "")


def _validate_no_change_needed(context: MigrationTaskContext) -> None:
    changing_rules = [
        rule
        for rule in context.matching_migration_rules
        if _rule_requires_source_change(rule)
    ]
    if not changing_rules:
        return

    source_by_path = {
        source.path: source.content for source in context.relevant_source_files
    }
    unresolved_files: set[str] = set()
    unresolved_usage_ids: set[str] = set()
    explicitly_updated = set(context.explicitly_updated_source_files)
    for rule in changing_rules:
        package_name = rule.source_package.rsplit("/", 1)[-1]
        package_markers = (rule.source_package, f"{package_name}.")
        for path in context.allowed_files:
            content = source_by_path.get(path, "")
            if any(marker in content for marker in package_markers):
                unresolved_files.add(path)
        for usage in context.direct_usages:
            if usage.file not in source_by_path:
                continue
            if (
                usage.symbol == rule.resolved_source_symbol
                and (
                    usage.file in unresolved_files
                    or usage.file not in explicitly_updated
                )
            ):
                unresolved_usage_ids.add(usage.usage_id)
                unresolved_files.add(usage.file)

    if unresolved_files:
        raise SuspiciousNoChangeProposalError(
            "validate_no_change_needed",
            "no_change_needed is inconsistent with migration rules and current "
            "source content",
            diagnostics={
                "rule_ids": ",".join(rule.rule_id for rule in changing_rules),
                "files": ",".join(sorted(unresolved_files)),
                "usage_ids": ",".join(sorted(unresolved_usage_ids)),
            },
        )


def _rule_requires_source_change(rule: MigrationRule) -> bool:
    if rule.target_operations:
        return True
    return (
        rule.source_package != rule.target_package
        or rule.source_symbol != rule.target_symbol
    )


def _parse_patch_paths(patch: str) -> set[str]:
    if not patch.strip():
        raise ProposalValidationError("validate_patch", "patch is empty")
    if "\x00" in patch or "GIT binary patch" in patch:
        raise ProposalValidationError(
            "validate_patch",
            "binary patches are not allowed",
        )
    affected: set[str] = set()
    current: tuple[str, str] | None = None
    old_header: str | None = None
    new_header: str | None = None
    has_hunk = False
    for line in patch.splitlines():
        if line.startswith("Binary files ") and line.endswith(" differ"):
            raise ProposalValidationError(
                "validate_patch",
                "binary patches are not allowed",
            )
        if line.startswith("diff --git "):
            if current is not None:
                _validate_section(current, old_header, new_header, has_hunk)
                affected.add(current[1])
            fields = _diff_header_fields(line)
            current = (
                _prefixed_path(fields[2], "a/"),
                _prefixed_path(fields[3], "b/"),
            )
            old_header = None
            new_header = None
            has_hunk = False
            continue
        if current is None:
            if line.strip():
                raise ProposalValidationError(
                    "validate_patch",
                    "patch must begin with a diff --git header",
                )
            continue
        if line.startswith(("new file mode ", "deleted file mode ")):
            raise ProposalValidationError(
                "validate_patch",
                "new-file and delete-file patches are not allowed",
            )
        if line.startswith(("rename from ", "rename to ")):
            raise ProposalValidationError(
                "validate_patch",
                "rename patches are not allowed",
            )
        if not has_hunk and line.startswith("--- "):
            old_header = _patch_header_path(line[4:], "a/")
        elif not has_hunk and line.startswith("+++ "):
            new_header = _patch_header_path(line[4:], "b/")
        elif line.startswith("@@ "):
            has_hunk = True
    if current is None:
        raise ProposalValidationError(
            "validate_patch",
            "patch contains no file changes",
        )
    _validate_section(current, old_header, new_header, has_hunk)
    affected.add(current[1])
    _validate_hunk_counts(patch)
    return affected


def _validate_hunk_counts(patch: str) -> None:
    lines = patch.splitlines()
    index = 0
    while index < len(lines):
        line = lines[index]
        if not line.startswith("@@ "):
            index += 1
            continue
        match = _HUNK_HEADER.match(line)
        if match is None:
            raise ProposalValidationError(
                "validate_patch",
                f"malformed hunk header: {line}",
            )
        expected_old = int(match.group("old") or "1")
        expected_new = int(match.group("new") or "1")
        actual_old = 0
        actual_new = 0
        context_lines = 0
        index += 1
        while index < len(lines) and not lines[index].startswith(
            ("@@ ", "diff --git ")
        ):
            body = lines[index]
            if body.startswith("\\ No newline at end of file"):
                index += 1
                continue
            if body.startswith(" "):
                actual_old += 1
                actual_new += 1
                context_lines += 1
            elif body.startswith("-") and not body.startswith("--- "):
                actual_old += 1
            elif body.startswith("+") and not body.startswith("+++ "):
                actual_new += 1
            index += 1
        if (actual_old, actual_new) != (expected_old, expected_new):
            raise ProposalValidationError(
                "validate_patch",
                "hunk line counts do not match its header: "
                f"expected old/new {expected_old}/{expected_new}, "
                f"found {actual_old}/{actual_new}",
            )
        if context_lines == 0:
            raise ProposalValidationError(
                "validate_patch",
                "each hunk must contain at least one unchanged context line",
            )


def _validate_section(
    paths: tuple[str, str],
    old_header: str | None,
    new_header: str | None,
    has_hunk: bool,
) -> None:
    old_path, new_path = paths
    if old_path != new_path:
        raise ProposalValidationError(
            "validate_patch",
            "rename patches are not allowed",
        )
    if old_header is None or new_header is None or not has_hunk:
        raise ProposalValidationError(
            "validate_patch",
            f"malformed patch section for {new_path}",
        )
    if old_header != old_path or new_header != new_path:
        raise ProposalValidationError(
            "validate_patch",
            f"patch headers do not match diff header for {new_path}",
        )


def _patch_header_path(value: str, prefix: str) -> str:
    token = value.split("\t", 1)[0].strip()
    if token == "/dev/null":
        kind = "new-file" if prefix == "a/" else "delete-file"
        raise ProposalValidationError(
            "validate_patch",
            f"{kind} patches are not allowed",
        )
    if token.startswith('"'):
        try:
            parsed = shlex.split(token, posix=True)
        except ValueError as error:
            raise ProposalValidationError(
                "validate_patch",
                f"malformed patch path: {token}",
            ) from error
        if len(parsed) != 1:
            raise ProposalValidationError(
                "validate_patch",
                f"malformed patch path: {token}",
            )
        token = parsed[0]
    return _prefixed_path(token, prefix)


def _diff_header_fields(line: str) -> list[str]:
    try:
        fields = shlex.split(line, posix=True)
    except ValueError as error:
        raise ProposalValidationError(
            "validate_patch",
            f"malformed diff header: {line}",
        ) from error
    if len(fields) != 4 or fields[:2] != ["diff", "--git"]:
        raise ProposalValidationError(
            "validate_patch",
            f"malformed diff header: {line}",
        )
    return fields


def _prefixed_path(value: str, prefix: str) -> str:
    if not value.startswith(prefix):
        raise ProposalValidationError(
            "validate_patch",
            f"patch path must start with {prefix}: {value}",
        )
    return _safe_path(value[len(prefix) :])


def _safe_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or path.is_absolute()
        or ".." in path.parts
        or any(part.casefold() == ".git" for part in path.parts)
        or (path.parts and path.parts[0].endswith(":"))
    ):
        raise ProposalValidationError(
            "validate_patch_path",
            f"unsafe patch path: {value}",
        )
    return path.as_posix()
