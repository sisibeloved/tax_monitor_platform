from tax_risk.adapters.ingest.business_entertainment_csv import (
    BusinessEntertainmentCsvAdapter,
)
from tax_risk.domain.semantic.sap_voucher import SapExpenseVoucherRecord


class SapBusinessEntertainmentCsvAdapter(BusinessEntertainmentCsvAdapter):
    HEADER = (
        "company_code", "fiscal_year", "period", "posting_date", "document_number",
        "line_item", "current_account_code", "current_account_name", "amount", "currency",
        "summary", "assignment", "reference", "reversal_reference",
    )
    DATASET_CODE = "sap_business_entertainment"
    SCHEMA_VERSION = "sap-business-entertainment-v1"
    PRIMARY_KEY_FIELDS = ("company_code", "fiscal_year", "document_number", "line_item")
    RECORD_TYPE = SapExpenseVoucherRecord


__all__ = ["SapBusinessEntertainmentCsvAdapter"]
