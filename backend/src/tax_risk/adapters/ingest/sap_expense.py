"""Shared SAP expense adapters for welfare and public-interest donations."""

from __future__ import annotations

from collections.abc import Iterator
from typing import ClassVar

from tax_risk.adapters.ingest.base import AdapterRow
from tax_risk.adapters.ingest.business_entertainment_csv import (
    BusinessEntertainmentCsvAdapter,
)
from tax_risk.domain.semantic.sap_voucher import AccountFamily, SapExpenseVoucherRecord


class SapExpenseCsvAdapter(BusinessEntertainmentCsvAdapter):
    HEADER = (
        "company_code",
        "fiscal_year",
        "period",
        "posting_date",
        "document_number",
        "line_item",
        "current_account_code",
        "current_account_name",
        "amount",
        "currency",
        "summary",
        "assignment",
        "reference",
        "reversal_reference",
    )
    PRIMARY_KEY_FIELDS = ("company_code", "fiscal_year", "document_number", "line_item")
    RECORD_TYPE = SapExpenseVoucherRecord
    ACCOUNT_FAMILY: ClassVar[AccountFamily]

    def iter_rows(self) -> Iterator[AdapterRow]:
        for adapted in super().iter_rows():
            value = adapted.value
            if not isinstance(value, SapExpenseVoucherRecord):
                yield adapted
                continue
            yield AdapterRow(
                row_number=adapted.row_number,
                value=value.model_copy(update={"account_family": self.ACCOUNT_FAMILY}),
                error=None,
            )


class SapWelfareCsvAdapter(SapExpenseCsvAdapter):
    DATASET_CODE = "SAP_WELFARE_DETAIL"
    SCHEMA_VERSION = "sap-welfare-detail-v1"
    ACCOUNT_FAMILY = AccountFamily.WELFARE


class SapDonationCsvAdapter(SapExpenseCsvAdapter):
    DATASET_CODE = "SAP_DONATION_DETAIL"
    SCHEMA_VERSION = "sap-donation-detail-v1"
    ACCOUNT_FAMILY = AccountFamily.DONATION


__all__ = ["SapDonationCsvAdapter", "SapExpenseCsvAdapter", "SapWelfareCsvAdapter"]
