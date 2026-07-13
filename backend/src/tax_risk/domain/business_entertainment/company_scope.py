from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class ScopeVersionStatus(StrEnum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    PUBLISHED = "PUBLISHED"
    RETIRED = "RETIRED"


class BusinessEntertainmentScopeVersion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version_id: UUID
    batch_id: UUID
    effective_from: date
    effective_to: date
    source_file_name: str
    file_checksum: str
    uploader_id: str
    reviewer_id: str | None
    status: ScopeVersionStatus
    published_at: datetime | None


class BusinessEntertainmentScopeResolution(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version_id: UUID
    effective_from: date
    effective_to: date
    company_codes: tuple[str, ...]


__all__ = [
    "BusinessEntertainmentScopeResolution",
    "BusinessEntertainmentScopeVersion",
    "ScopeVersionStatus",
]
