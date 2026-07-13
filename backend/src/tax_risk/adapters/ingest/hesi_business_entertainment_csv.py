from tax_risk.adapters.ingest.business_entertainment_csv import (
    BusinessEntertainmentCsvAdapter,
)
from tax_risk.domain.business_entertainment.source_models import (
    HesiBusinessEntertainmentRecord,
)


class HesiBusinessEntertainmentCsvAdapter(BusinessEntertainmentCsvAdapter):
    HEADER = (
        "company_code", "fiscal_year", "period", "expense_claim_id", "line_id",
        "expense_date", "amount", "currency", "summary", "expense_reason",
        "recipient_category", "participant_count", "related_oa_id",
        "sap_document_number", "sap_line_item",
    )
    DATASET_CODE = "hesi_business_entertainment"
    SCHEMA_VERSION = "hesi-business-entertainment-v1"
    PRIMARY_KEY_FIELDS = ("company_code", "expense_claim_id", "line_id")
    RECORD_TYPE = HesiBusinessEntertainmentRecord


__all__ = ["HesiBusinessEntertainmentCsvAdapter"]
