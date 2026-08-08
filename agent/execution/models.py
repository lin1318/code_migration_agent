"""Typed execution context and results."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict



class ExecutionModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RelevantSourceFile(ExecutionModel):
    path: str
    content: str
