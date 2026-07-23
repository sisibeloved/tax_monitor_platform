"""API contracts for deterministic income-tax refund monitoring."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_serializer, field_validator


MoneyDecimal = Annotated[Decimal, Field(gt=0, max_digits=38, decimal_places=12)]


class IncomeTaxRefundTargetInput(BaseModel):
    company_code: str = Field(min_length=1, max_length=64)
    source_record_key: str = Field(min_length=1, max_length=256)
    expected_refund_amount: MoneyDecimal
    raw_expected_refund_amount: MoneyDecimal | None = None
    currency: str = Field(default="CNY", pattern=r"^[A-Z]{3}$")
    amount_scale: int = Field(default=2, ge=0, le=12)
    received_in_source: bool = False

    model_config = ConfigDict(extra="forbid")

    @field_validator("company_code", "source_record_key")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        return value.strip()


class IncomeTaxRefundTargetImportRequest(BaseModel):
    refund_tax_year: int = Field(ge=2000, le=9998)
    source_version: str = Field(min_length=1, max_length=128)
    items: tuple[IncomeTaxRefundTargetInput, ...] = Field(min_length=1, max_length=5000)

    model_config = ConfigDict(extra="forbid")

    @field_validator("source_version")
    @classmethod
    def normalize_source_version(cls, value: str) -> str:
        return value.strip()


class IncomeTaxRefundSapLineInput(BaseModel):
    company_code: str = Field(min_length=1, max_length=64)
    client: str = Field(min_length=1, max_length=16)
    ledger: str = Field(min_length=1, max_length=16)
    fiscal_year: int = Field(ge=2000, le=9999)
    fiscal_period: int = Field(ge=1, le=12)
    posting_date: date
    document_number: str = Field(min_length=1, max_length=64)
    line_item: str = Field(min_length=1, max_length=32)
    gl_account_code: str = Field(min_length=1, max_length=64)
    gl_account_name: str = Field(min_length=1, max_length=256)
    account_category: Literal[
        "INCOME_TAX_EXPENSE",
        "OTHER_INCOME",
        "TAXES_PAYABLE",
    ]
    debit_credit: Literal["DEBIT", "CREDIT"]
    amount: MoneyDecimal
    currency: str = Field(default="CNY", pattern=r"^[A-Z]{3}$")
    amount_scale: int = Field(default=2, ge=0, le=12)
    is_reversed: bool = False

    model_config = ConfigDict(extra="forbid")

    @field_validator(
        "company_code",
        "client",
        "ledger",
        "document_number",
        "line_item",
        "gl_account_code",
        "gl_account_name",
    )
    @classmethod
    def normalize_line_text(cls, value: str) -> str:
        return value.strip()


class IncomeTaxRefundSapEvidenceImportRequest(BaseModel):
    source_batch_key: str = Field(min_length=1, max_length=256)
    fiscal_year: int = Field(ge=2000, le=9999)
    through_period: int = Field(ge=1, le=12)
    company_codes: tuple[str, ...] = Field(min_length=1, max_length=5000)
    items: tuple[IncomeTaxRefundSapLineInput, ...] = Field(default=(), max_length=100_000)

    model_config = ConfigDict(extra="forbid")

    @field_validator("source_batch_key")
    @classmethod
    def normalize_batch_key(cls, value: str) -> str:
        return value.strip()

    @field_validator("company_codes")
    @classmethod
    def normalize_company_codes(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(value.strip() for value in values)
        if any(not value for value in normalized):
            raise ValueError("company_codes must not contain blank values")
        if len(set(normalized)) != len(normalized):
            raise ValueError("company_codes must be unique")
        return normalized


class IncomeTaxRefundScanRequest(BaseModel):
    refund_tax_year: int = Field(ge=2000, le=9998)
    scan_year: int = Field(ge=2001, le=9999)
    scan_month: int = Field(ge=3, le=12)
    source_batch_key: str = Field(min_length=1, max_length=256)

    model_config = ConfigDict(extra="forbid")

    @field_validator("source_batch_key")
    @classmethod
    def normalize_scan_batch_key(cls, value: str) -> str:
        return value.strip()


class IncomeTaxRefundImportResponse(BaseModel):
    source_version: str
    accepted_count: int
    replayed_count: int


class IncomeTaxRefundSapEvidenceResponse(BaseModel):
    source_batch_key: str
    accepted_count: int
    replayed_count: int
    complete_company_count: int


class IncomeTaxRefundScanItemResponse(BaseModel):
    target_id: UUID
    company_id: UUID
    company_code: str
    company_name: str
    refund_tax_year: int
    scan_period: str
    expected_refund_amount: Decimal
    currency: str
    receipt_status: Literal["NOT_RECEIVED", "RECEIVED", "AMBIGUOUS"]
    booking_status: Literal["NOT_APPLICABLE", "CORRECT", "WRONG_ACCOUNT", "AMBIGUOUS"]
    account_family: (
        Literal[
            "INCOME_TAX_EXPENSE",
            "OTHER_INCOME",
            "TAXES_PAYABLE",
        ]
        | None
    )
    receipt_source: Literal["SAP_MATCH", "LARK_MANUAL"]
    matched_amount: Decimal | None
    gl_account_code: str | None
    gl_account_name: str | None
    document_number: str | None
    line_item: str | None
    posting_date: date | None
    alert_code: Literal[
        "REFUND_BOOKED_TO_WRONG_ACCOUNT",
        "AMBIGUOUS_REFUND_MATCH",
    ] | None
    writeback_status: Literal["PENDING", "PROCESSING", "SUCCEEDED", "FAILED"] | None

    @field_serializer("expected_refund_amount", "matched_amount")
    def serialize_amount(self, value: Decimal | None) -> str | None:
        return None if value is None else format(value, "f")


class IncomeTaxRefundScanResponse(BaseModel):
    refund_tax_year: int
    scan_period: str
    received_count: int
    not_received_count: int
    wrong_account_count: int
    ambiguous_count: int
    received: tuple[IncomeTaxRefundScanItemResponse, ...]
    not_received: tuple[IncomeTaxRefundScanItemResponse, ...]
    ambiguous: tuple[IncomeTaxRefundScanItemResponse, ...]


__all__ = [
    "IncomeTaxRefundImportResponse",
    "IncomeTaxRefundSapEvidenceImportRequest",
    "IncomeTaxRefundSapEvidenceResponse",
    "IncomeTaxRefundSapLineInput",
    "IncomeTaxRefundScanItemResponse",
    "IncomeTaxRefundScanRequest",
    "IncomeTaxRefundScanResponse",
    "IncomeTaxRefundTargetImportRequest",
    "IncomeTaxRefundTargetInput",
]
