from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tax_risk.persistence.ingest_models import IngestBatchStatus, IngestMode


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


__all__ = ["IngestBatchCreate", "IngestBatchResponse", "IngestErrorResponse"]
