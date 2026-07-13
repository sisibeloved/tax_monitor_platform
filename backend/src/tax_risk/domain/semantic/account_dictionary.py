"""Governed suggested-account dictionary contracts shared by semantic monitors."""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from tax_risk.domain.cases import MonitorType
from tax_risk.domain.semantic.contracts import SemanticLabel


class AccountDictionaryVersionStatus(StrEnum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    PUBLISHED = "PUBLISHED"
    RETIRED = "RETIRED"


class AccountEntryStatus(StrEnum):
    ACTIVE = "ACTIVE"
    INACTIVE = "INACTIVE"


class SuggestedAccountEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    account_id: str = Field(min_length=1, max_length=128)
    account_code: str = Field(min_length=1, max_length=64)
    account_name: str = Field(min_length=1, max_length=256)
    accounting_classification: str = Field(min_length=1, max_length=128)
    allowed_monitor_types: tuple[MonitorType, ...] = Field(min_length=1)
    allowed_labels: tuple[SemanticLabel, ...] = Field(min_length=1)
    status: AccountEntryStatus


class SuggestedAccountDictionaryVersion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    version_id: UUID
    batch_id: UUID
    dictionary_version: str = Field(min_length=1, max_length=128)
    effective_from: date
    effective_to: date
    checksum: str = Field(min_length=64, max_length=64)
    uploaded_by: str = Field(min_length=1, max_length=256)
    reviewer_id: str | None = Field(default=None, max_length=256)
    published_by: str | None = Field(default=None, max_length=256)
    status: AccountDictionaryVersionStatus
    approved_at: datetime | None
    published_at: datetime | None

    @model_validator(mode="after")
    def state_and_period_are_consistent(self) -> SuggestedAccountDictionaryVersion:
        if self.effective_to < self.effective_from:
            raise ValueError("effective_to must not precede effective_from")
        if self.status is AccountDictionaryVersionStatus.PUBLISHED:
            if self.reviewer_id is None or self.published_at is None:
                raise ValueError("published dictionary requires review and publication time")
        return self


__all__ = [
    "AccountDictionaryVersionStatus",
    "AccountEntryStatus",
    "SuggestedAccountDictionaryVersion",
    "SuggestedAccountEntry",
]
