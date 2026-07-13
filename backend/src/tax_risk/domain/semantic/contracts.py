"""Strict separation between model judgment and server-owned detection facts."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SemanticLabel(StrEnum):
    CURRENT_ACCOUNT_REASONABLE = "CURRENT_ACCOUNT_REASONABLE"
    MEETING_EXPENSE = "MEETING_EXPENSE"
    EMPLOYEE_EDUCATION = "EMPLOYEE_EDUCATION"
    EMPLOYEE_WELFARE = "EMPLOYEE_WELFARE"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


class ConfidenceTier(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"


class EvidenceCitation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    evidence_id: str = Field(min_length=1, max_length=128)
    field_name: str = Field(min_length=1, max_length=128)
    quoted_text: str = Field(min_length=1, max_length=1000)


class EvidenceRef(EvidenceCitation):
    source_record_id: UUID
    snapshot_id: UUID


class SemanticModelJudgment(BaseModel):
    """The only schema a model may produce; it contains no authority fields."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    semantic_label: SemanticLabel
    confidence_tier: ConfidenceTier
    evidence_citations: list[EvidenceCitation]
    recommended_account_ids: list[str]
    rationale_summary: str = Field(min_length=1, max_length=1000)
    missing_evidence: list[str]

    @field_validator("recommended_account_ids", "missing_evidence")
    @classmethod
    def unique_nonempty_strings(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("list members must be non-empty")
        if len(value) != len(set(value)):
            raise ValueError("list members must be unique")
        return value


class SemanticVersionSet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    rule_version_id: str = Field(min_length=1, max_length=128)
    model_version_id: str = Field(min_length=1, max_length=128)
    prompt_version_id: str = Field(min_length=1, max_length=128)
    case_library_version_id: str = Field(min_length=1, max_length=128)
    account_dictionary_version: str = Field(min_length=1, max_length=128)


class SemanticDetection(BaseModel):
    """Authoritative result assembled only after citation and account validation."""

    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    detection_key: str = Field(min_length=1, max_length=512)
    candidate_key: str = Field(min_length=1, max_length=512)
    company_code: str = Field(min_length=1, max_length=64)
    fiscal_year: int = Field(ge=2000, le=9999)
    period: int = Field(ge=1, le=12)
    source_mode: str = Field(pattern=r"^(SAP_LINKED|BUSINESS_DOCUMENT_UNLINKED)$")
    canonical_source_record_id: UUID
    sap_observation_id: UUID | None
    sap_document_number: str | None = Field(default=None, max_length=64)
    sap_line_item: str | None = Field(default=None, max_length=32)
    amount: Decimal
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    snapshot_id: UUID
    versions: SemanticVersionSet
    semantic_label: SemanticLabel
    confidence_tier: ConfidenceTier
    evidence_refs: list[EvidenceRef]
    recommended_account_ids: list[str]
    rationale_summary: str = Field(min_length=1, max_length=1000)
    missing_evidence: list[str]
    detected_at: datetime

    @field_validator("amount")
    @classmethod
    def finite_amount(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("amount must be finite")
        return value

    @model_validator(mode="after")
    def source_mode_owns_sap_identifiers(self) -> SemanticDetection:
        sap_fields = (
            self.sap_observation_id,
            self.sap_document_number,
            self.sap_line_item,
        )
        if self.source_mode == "SAP_LINKED" and any(value is None for value in sap_fields):
            raise ValueError("SAP_LINKED detection requires all SAP identifiers")
        if self.source_mode == "BUSINESS_DOCUMENT_UNLINKED" and any(
            value is not None for value in sap_fields
        ):
            raise ValueError("unlinked detection cannot contain SAP identifiers")
        if self.detected_at.tzinfo is None or self.detected_at.utcoffset() is None:
            raise ValueError("detected_at must be timezone-aware")
        return self


__all__ = [
    "ConfidenceTier",
    "EvidenceCitation",
    "EvidenceRef",
    "SemanticDetection",
    "SemanticLabel",
    "SemanticModelJudgment",
    "SemanticVersionSet",
]
