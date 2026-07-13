"""Strict versioned candidate lexicon contracts."""

from __future__ import annotations

from datetime import date
from enum import StrEnum
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class LexiconStatus(StrEnum):
    DRAFT = "DRAFT"
    PUBLISHED = "PUBLISHED"
    RETIRED = "RETIRED"


class CandidateSignal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    signal_id: str = Field(min_length=1, max_length=128)
    canonical_phrase: str = Field(min_length=1, max_length=256)
    aliases: tuple[str, ...] = Field(min_length=1)
    allowed_fields: tuple[str, ...] = Field(min_length=1)
    priority: int = Field(ge=1, le=1000)
    label_hints: tuple[str, ...] = Field(min_length=1)

    @field_validator("aliases", "allowed_fields", "label_hints")
    @classmethod
    def members_are_nonempty_and_unique(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("list members must be non-empty")
        if len(value) != len(set(value)):
            raise ValueError("list members must be unique")
        return value


class CandidateLexicon(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    version: str = Field(min_length=1, max_length=64)
    monitor_type: str = Field(pattern=r"^BUSINESS_ENTERTAINMENT$")
    effective_from: date
    status: LexiconStatus
    signals: tuple[CandidateSignal, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def signal_ids_are_unique(self) -> CandidateLexicon:
        signal_ids = [signal.signal_id for signal in self.signals]
        if len(signal_ids) != len(set(signal_ids)):
            raise ValueError("duplicate signal_id")
        return self


def load_lexicon(path: Path) -> CandidateLexicon:
    """Load a JSON-form YAML 1.2 document through the strict schema."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    return CandidateLexicon.model_validate(payload)


__all__ = [
    "CandidateLexicon",
    "CandidateSignal",
    "LexiconStatus",
    "load_lexicon",
]
