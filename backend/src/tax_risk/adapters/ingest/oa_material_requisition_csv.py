from tax_risk.adapters.ingest.business_entertainment_csv import (
    BusinessEntertainmentCsvAdapter,
)
from tax_risk.domain.business_entertainment.source_models import OaMaterialRequisitionRecord


class OaMaterialRequisitionCsvAdapter(BusinessEntertainmentCsvAdapter):
    HEADER = (
        "company_code", "requisition_id", "line_id", "requisition_date",
        "material_description", "purpose", "recipient_category", "quantity", "unit",
        "amount", "currency", "parent_oa_id", "parent_hesi_id",
    )
    DATASET_CODE = "oa_material_requisition"
    SCHEMA_VERSION = "oa-material-requisition-v1"
    PRIMARY_KEY_FIELDS = ("company_code", "requisition_id", "line_id")
    RECORD_TYPE = OaMaterialRequisitionRecord


__all__ = ["OaMaterialRequisitionCsvAdapter"]
