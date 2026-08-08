"""Structured model output for one migration task."""

from __future__ import annotations

from enum import StrEnum


class PatchConfidence(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
