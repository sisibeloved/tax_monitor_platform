"""Immutable contracts for business-entertainment evaluation inputs."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class EvaluationSourceMode(StrEnum):
    SAP_LINKED = "SAP_LINKED"
    BUSINESS_DOCUMENT_UNLINKED = "BUSINESS_DOCUMENT_UNLINKED"


class CanonicalRecordType(StrEnum):
    HESI = "HESI"
    OA = "OA"


class AmountSource(StrEnum):
    SAP = "SAP"
    HESI = "HESI"
    OA = "OA"


class SapLinkStatus(StrEnum):
    LINKED = "LINKED"
    UNLINKED = "UNLINKED"


class BusinessEntertainmentEvaluationItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    candidate_key: str = Field(min_length=1, max_length=512)
    company_code: str = Field(min_length=1, max_length=64)
    fiscal_year: int = Field(ge=2000, le=9999)
    period: int = Field(ge=1, le=12)
    source_mode: EvaluationSourceMode
    canonical_record_type: CanonicalRecordType
    canonical_source_record_id: UUID
    canonical_business_key: str = Field(min_length=1, max_length=512)
    sap_observation_id: UUID | None
    sap_business_key: str | None = Field(default=None, max_length=512)
    sap_document_number: str | None = Field(default=None, max_length=64)
    sap_line_item: str | None = Field(default=None, max_length=32)
    current_account_code: str | None = Field(default=None, max_length=64)
    current_account_name: str | None = Field(default=None, max_length=256)
    amount: Decimal
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    amount_source: AmountSource
    exact_evidence_link_id: UUID | None
    snapshot_id: UUID

    @field_validator("amount")
    @classmethod
    def finite_amount(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("amount must be finite")
        return value

    @model_validator(mode="after")
    def source_mode_is_consistent(self) -> BusinessEntertainmentEvaluationItem:
        sap_fields = (
            self.sap_observation_id,
            self.sap_business_key,
            self.sap_document_number,
            self.sap_line_item,
            self.current_account_code,
            self.current_account_name,
        )
        if self.source_mode is EvaluationSourceMode.SAP_LINKED:
            if any(value is None for value in sap_fields):
                raise ValueError("SAP fields are required in SAP_LINKED mode")
            if self.exact_evidence_link_id is None:
                raise ValueError("exact evidence is required in SAP_LINKED mode")
            if self.amount_source is not AmountSource.SAP:
                raise ValueError("SAP_LINKED amount must come from SAP")
        elif any(value is not None for value in sap_fields):
            raise ValueError("SAP fields are forbidden in BUSINESS_DOCUMENT_UNLINKED mode")

        expected_amount_source = {
            CanonicalRecordType.HESI: AmountSource.HESI,
            CanonicalRecordType.OA: AmountSource.OA,
        }[self.canonical_record_type]
        if (
            self.source_mode is EvaluationSourceMode.BUSINESS_DOCUMENT_UNLINKED
            and self.amount_source is not expected_amount_source
        ):
            raise ValueError("unlinked amount source must match the canonical document")
        return self


class SapLinkCoverageItem(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", str_strip_whitespace=True)

    company_code: str = Field(min_length=1, max_length=64)
    period_end: date
    sap_observation_id: UUID
    document_number: str = Field(min_length=1, max_length=64)
    line_item: str = Field(min_length=1, max_length=32)
    amount: Decimal
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    link_status: SapLinkStatus
    exact_evidence_link_id: UUID | None
    evaluated_via_business_document: bool
    snapshot_id: UUID

    @field_validator("amount")
    @classmethod
    def finite_coverage_amount(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("amount must be finite")
        return value

    @model_validator(mode="after")
    def link_state_is_consistent(self) -> SapLinkCoverageItem:
        if self.link_status is SapLinkStatus.LINKED:
            if self.exact_evidence_link_id is None or not self.evaluated_via_business_document:
                raise ValueError("LINKED coverage requires exact evidence and evaluation")
        elif self.exact_evidence_link_id is not None or self.evaluated_via_business_document:
            raise ValueError("UNLINKED coverage cannot claim exact business evidence")
        return self


__all__ = [
    "AmountSource",
    "BusinessEntertainmentEvaluationItem",
    "CanonicalRecordType",
    "EvaluationSourceMode",
    "SapLinkCoverageItem",
    "SapLinkStatus",
]
