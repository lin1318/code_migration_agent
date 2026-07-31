"""Template-backed prompt construction."""

from __future__ import annotations

from pathlib import Path
from string import Template

from agent.execution.errors import PromptBuildError
from agent.execution.models import MigrationTaskContext
from models.migration_plan import MigrationPhase

PROMPT_ROOT = Path(__file__).resolve().parents[2] / "prompts"


def build_prompt(context: MigrationTaskContext) -> str:
    phase = context.selected_migration_task.phase
    if phase != MigrationPhase.ROUTING:
        raise PromptBuildError(
            "build_prompt",
            f"no Stage 8 prompt template for phase: {phase.value}",
        )
    template_path = PROMPT_ROOT / "routing.md"
    try:
        template_text = template_path.read_text(encoding="utf-8")
    except OSError as error:
        raise PromptBuildError(
            "build_prompt",
            f"cannot load prompt template {template_path}: {error}",
        ) from error
    try:
        return Template(template_text).substitute(
            context_json=context.model_dump_json(indent=2),
        )
    except (KeyError, ValueError) as error:
        raise PromptBuildError(
            "build_prompt",
            f"invalid prompt template {template_path}: {error}",
        ) from error
