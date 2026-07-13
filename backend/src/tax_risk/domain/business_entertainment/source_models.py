from __future__ import annotations

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class _SourceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    company_code: str = Field(min_length=1, max_length=64)

    @field_validator("amount", check_fields=False)
    @classmethod
    def finite_amount(cls, value: Decimal | None) -> Decimal | None:
        if value is not None and not value.is_finite():
            raise ValueError("amount must be finite")
        return value


class HesiBusinessEntertainmentRecord(_SourceRecord):
    fiscal_year: int = Field(ge=2000, le=9999)
    period: int = Field(ge=1, le=12)
    expense_claim_id: str = Field(min_length=1, max_length=128)
    line_id: str = Field(min_length=1, max_length=64)
    expense_date: date
    amount: Decimal
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    summary: str = Field(min_length=1, max_length=2000)
    expense_reason: str = Field(min_length=1, max_length=4000)
    recipient_category: str = Field(min_length=1, max_length=128)
    participant_count: int = Field(ge=1)
    related_oa_id: str | None = Field(default=None, max_length=128)
    sap_document_number: str | None = Field(default=None, max_length=64)
    sap_line_item: str | None = Field(default=None, max_length=32)

    @model_validator(mode="after")
    def expense_period_matches(self) -> HesiBusinessEntertainmentRecord:
        if self.expense_date.year != self.fiscal_year or self.expense_date.month != self.period:
            raise ValueError("expense_date must match fiscal_year and period")
        if (self.sap_document_number is None) != (self.sap_line_item is None):
            raise ValueError("direct SAP document and line item must be supplied together")
        return self

    @property
    def source_record_key(self) -> str:
        return "|".join((self.company_code, self.expense_claim_id, self.line_id))


class OaBusinessEntertainmentRecord(_SourceRecord):
    application_id: str = Field(min_length=1, max_length=128)
    line_id: str = Field(min_length=1, max_length=64)
    application_date: date
    reason: str = Field(min_length=1, max_length=4000)
    recipient_category: str = Field(min_length=1, max_length=128)
    participant_count: int = Field(ge=1)
    amount: Decimal
    currency: str = Field(pattern=r"^[A-Z]{3}$")

    @property
    def source_record_key(self) -> str:
        return "|".join((self.company_code, self.application_id, self.line_id))


class OaSelfProcurementRecord(_SourceRecord):
    application_id: str = Field(min_length=1, max_length=128)
    line_id: str = Field(min_length=1, max_length=64)
    purchase_date: date
    item_description: str = Field(min_length=1, max_length=2000)
    reason: str = Field(min_length=1, max_length=4000)
    recipient_category: str = Field(min_length=1, max_length=128)
    amount: Decimal
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    parent_oa_id: str | None = Field(default=None, max_length=128)
    parent_hesi_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def parent_is_required(self) -> OaSelfProcurementRecord:
        if self.parent_oa_id is None and self.parent_hesi_id is None:
            raise ValueError("an exact parent OA or Hesi id is required")
        return self

    @property
    def source_record_key(self) -> str:
        return "|".join((self.company_code, self.application_id, self.line_id))


class OaMaterialRequisitionRecord(_SourceRecord):
    requisition_id: str = Field(min_length=1, max_length=128)
    line_id: str = Field(min_length=1, max_length=64)
    requisition_date: date
    material_description: str = Field(min_length=1, max_length=2000)
    purpose: str = Field(min_length=1, max_length=4000)
    recipient_category: str = Field(min_length=1, max_length=128)
    quantity: Decimal = Field(gt=0)
    unit: str = Field(min_length=1, max_length=64)
    amount: Decimal | None = None
    currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    parent_oa_id: str | None = Field(default=None, max_length=128)
    parent_hesi_id: str | None = Field(default=None, max_length=128)

    @model_validator(mode="after")
    def optional_amount_and_parent_are_consistent(self) -> OaMaterialRequisitionRecord:
        if (self.amount is None) != (self.currency is None):
            raise ValueError("amount and currency must be supplied together")
        if self.parent_oa_id is None and self.parent_hesi_id is None:
            raise ValueError("an exact parent OA or Hesi id is required")
        return self

    @property
    def source_record_key(self) -> str:
        return "|".join((self.company_code, self.requisition_id, self.line_id))


BusinessEntertainmentSourceRecord = (
    HesiBusinessEntertainmentRecord
    | OaBusinessEntertainmentRecord
    | OaSelfProcurementRecord
    | OaMaterialRequisitionRecord
)


__all__ = [
    "BusinessEntertainmentSourceRecord",
    "HesiBusinessEntertainmentRecord",
    "OaBusinessEntertainmentRecord",
    "OaMaterialRequisitionRecord",
    "OaSelfProcurementRecord",
]
