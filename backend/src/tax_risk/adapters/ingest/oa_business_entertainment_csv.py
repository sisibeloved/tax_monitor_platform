from tax_risk.adapters.ingest.business_entertainment_csv import (
    BusinessEntertainmentCsvAdapter,
)
from tax_risk.domain.business_entertainment.source_models import (
    OaBusinessEntertainmentRecord,
)


class OaBusinessEntertainmentCsvAdapter(BusinessEntertainmentCsvAdapter):
    HEADER = (
        "company_code", "application_id", "line_id", "application_date", "reason",
        "recipient_category", "participant_count", "amount", "currency",
    )
    DATASET_CODE = "oa_business_entertainment"
    SCHEMA_VERSION = "oa-business-entertainment-v1"
    PRIMARY_KEY_FIELDS = ("company_code", "application_id", "line_id")
    RECORD_TYPE = OaBusinessEntertainmentRecord


__all__ = ["OaBusinessEntertainmentCsvAdapter"]
