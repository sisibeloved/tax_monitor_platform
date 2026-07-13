from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class AccountFamily(StrEnum):
    BUSINESS_ENTERTAINMENT = "BUSINESS_ENTERTAINMENT"


class SapExpenseVoucherRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    company_code: str = Field(min_length=1, max_length=64)
    fiscal_year: int = Field(ge=2000, le=9999)
    period: int = Field(ge=1, le=12)
    posting_date: date
    document_number: str = Field(min_length=1, max_length=64)
    line_item: str = Field(min_length=1, max_length=32)
    current_account_code: str = Field(min_length=1, max_length=64)
    current_account_name: str = Field(min_length=1, max_length=256)
    amount: Decimal
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    summary: str = Field(min_length=1, max_length=2000)
    assignment: str | None = Field(default=None, max_length=256)
    reference: str | None = Field(default=None, max_length=256)
    reversal_reference: str | None = Field(default=None, max_length=256)
    account_family: AccountFamily = AccountFamily.BUSINESS_ENTERTAINMENT

    @field_validator("amount")
    @classmethod
    def finite_amount(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("amount must be finite")
        return value

    @model_validator(mode="after")
    def posting_period_matches(self) -> SapExpenseVoucherRecord:
        if self.posting_date.year != self.fiscal_year or self.posting_date.month != self.period:
            raise ValueError("posting_date must match fiscal_year and period")
        return self

    @property
    def source_record_key(self) -> str:
        return "|".join(
            (self.company_code, str(self.fiscal_year), self.document_number, self.line_item)
        )


class SnapshotBoundSapExpenseVoucher(SapExpenseVoucherRecord):
    projection_id: UUID
    snapshot_id: UUID
    observation_id: UUID
    source_record_id: UUID


__all__ = [
    "AccountFamily",
    "SapExpenseVoucherRecord",
    "SnapshotBoundSapExpenseVoucher",
]
