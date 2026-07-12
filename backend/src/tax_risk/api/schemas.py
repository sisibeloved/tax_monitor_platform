from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tax_risk.persistence.ingest_models import IngestBatchStatus, IngestMode
from tax_risk.persistence.master_models import VersionStatus
from tax_risk.persistence.snapshot_models import SnapshotSetStatus, SnapshotStatus
from tax_risk.snapshot_limits import (
    MAX_SNAPSHOT_SET_MEMBERS,
    MAX_SNAPSHOT_SOURCE_BATCHES,
)


class IngestBatchCreate(BaseModel):
    source: str = Field(min_length=1, max_length=64)
    source_batch_key: str = Field(min_length=1, max_length=256)
    dataset_code: str = Field(min_length=1, max_length=128)
    extraction_time: datetime
    period: date
    mode: IngestMode
    schema_version: str = Field(min_length=1, max_length=64)
    currency: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    amount_scale: int = Field(ge=0, le=12)
    source_primary_key_definition: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source", "source_batch_key", "dataset_code", "schema_version")
    @classmethod
    def strip_nonempty_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must not be blank")
        return stripped

    @field_validator("extraction_time")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("extraction_time must include a UTC offset")
        return value


class IngestErrorResponse(BaseModel):
    row_number: int
    error_code: str
    message: str
    details: dict[str, Any]
    retryable: bool

    model_config = ConfigDict(from_attributes=True)


class IngestBatchResponse(BaseModel):
    id: UUID
    source: str
    source_batch_key: str
    dataset_code: str
    status: IngestBatchStatus
    extraction_time: datetime
    period: date
    mode: IngestMode
    schema_version: str
    payload_ref: str | None
    source_primary_key_definition: dict[str, Any]
    currency: str
    amount_scale: int
    record_count: int
    accepted_count: int
    rejected_count: int
    control_total: Decimal
    checksum: str
    errors: tuple[IngestErrorResponse, ...]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TaxMasterImportResponse(BaseModel):
    batch_id: UUID
    checksum: str
    source_filename: str
    uploaded_by: str
    currency: str
    amount_scale: int
    version_ids: tuple[UUID, ...]
    imported_at: datetime
    replayed: bool

    model_config = ConfigDict(from_attributes=True)


class TaxMasterApproveRequest(BaseModel):
    reviewed_by: str = Field(min_length=1, max_length=256)

    @field_validator("reviewed_by")
    @classmethod
    def require_nonblank_reviewer(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reviewed_by must not be blank")
        return value


class TaxMasterResponse(BaseModel):
    id: UUID
    source_batch_id: UUID
    company_code: str
    company_name: str
    valid_from: date
    valid_to: date | None
    version: str
    status: VersionStatus
    tax_rate: Decimal
    loss_carryforward: Decimal
    three_year_average_tax_burden: Decimal
    currency: str
    amount_scale: int
    source_filename: str | None
    source_checksum: str | None
    source_row_number: int
    uploaded_by: str
    imported_at: datetime
    published_at: datetime | None
    approved_by: str | None

    model_config = ConfigDict(from_attributes=True)


class SnapshotValidateRequest(BaseModel):
    company_code: str
    period: date
    source_batch_ids: tuple[UUID, ...] = Field(
        max_length=MAX_SNAPSHOT_SOURCE_BATCHES
    )
    accepted_partial_batch_ids: tuple[UUID, ...] = Field(
        default=(),
        max_length=MAX_SNAPSHOT_SOURCE_BATCHES,
    )


class SnapshotQualityIssueResponse(BaseModel):
    category: str
    error_code: str
    source: str
    field: str
    company: str
    period: date
    remediation: str

    model_config = ConfigDict(from_attributes=True)


class SnapshotResponse(BaseModel):
    id: UUID
    company_id: UUID
    company_code: str
    tax_master_version_id: UUID
    period: date
    source_version_set_hash: str
    status: SnapshotStatus
    currency: str
    amount_scale: int
    record_count: int
    control_total: Decimal
    checksum: str
    lineage: dict[str, Any]
    published_at: datetime | None

    model_config = ConfigDict(from_attributes=True)


class SnapshotValidationResponse(BaseModel):
    valid: bool
    issues: tuple[SnapshotQualityIssueResponse, ...]
    snapshot: SnapshotResponse | None
    reused: bool

    model_config = ConfigDict(from_attributes=True)


class SnapshotSetMemberRequest(BaseModel):
    company_id: UUID
    snapshot_id: UUID


class SnapshotSetCreateRequest(BaseModel):
    set_key: str
    period: date
    expected_members: tuple[SnapshotSetMemberRequest, ...] = Field(
        max_length=MAX_SNAPSHOT_SET_MEMBERS
    )
    supersedes_snapshot_set_id: UUID | None = None


class SnapshotSetResponse(BaseModel):
    id: UUID
    set_key: str
    period: date
    status: SnapshotSetStatus
    expected_member_count: int
    published_at: datetime
    supersedes_snapshot_set_id: UUID | None
    members: tuple[SnapshotSetMemberRequest, ...]

    model_config = ConfigDict(from_attributes=True)


__all__ = [
    "IngestBatchCreate",
    "IngestBatchResponse",
    "IngestErrorResponse",
    "TaxMasterApproveRequest",
    "TaxMasterImportResponse",
    "TaxMasterResponse",
    "SnapshotQualityIssueResponse",
    "SnapshotResponse",
    "SnapshotSetCreateRequest",
    "SnapshotSetMemberRequest",
    "SnapshotSetResponse",
    "SnapshotValidateRequest",
    "SnapshotValidationResponse",
]
