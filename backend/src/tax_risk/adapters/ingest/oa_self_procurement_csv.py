from tax_risk.adapters.ingest.business_entertainment_csv import (
    BusinessEntertainmentCsvAdapter,
)
from tax_risk.domain.business_entertainment.source_models import OaSelfProcurementRecord


class OaSelfProcurementCsvAdapter(BusinessEntertainmentCsvAdapter):
    HEADER = (
        "company_code", "application_id", "line_id", "purchase_date", "item_description",
        "reason", "recipient_category", "amount", "currency", "parent_oa_id",
        "parent_hesi_id",
    )
    DATASET_CODE = "oa_self_procurement"
    SCHEMA_VERSION = "oa-self-procurement-v1"
    PRIMARY_KEY_FIELDS = ("company_code", "application_id", "line_id")
    RECORD_TYPE = OaSelfProcurementRecord


__all__ = ["OaSelfProcurementCsvAdapter"]
