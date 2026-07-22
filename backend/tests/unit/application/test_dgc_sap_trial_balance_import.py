from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest
from pydantic import ValidationError

from tax_risk.adapters.ingest.base import BulkFileAdapter, CanonicalFinancialRow
from tax_risk.adapters.ingest.dgc_sap_profit import DgcFetchResult, DgcTransportError
from tax_risk.adapters.ingest.dgc_sap_trial_balance import CURRENT_INCOME_TAX_GL_ACCOUNT
from tax_risk.api.schemas import DgcSapTrialBalanceImportRequest
from tax_risk.application.dgc_sap_trial_balance import (
    DgcSapTrialBalanceImportCommand,
    DgcSapTrialBalanceImportService,
    DgcTrialBalanceParameterValue,
    dgc_trial_balance_scope_sha256,
)
from tax_risk.application.ingest import (
    BatchMetadata,
    BatchView,
    CreateBatchResult,
    IngestService,
)
from tax_risk.persistence.ingest_models import IngestMode


class _Source:
    def __init__(self, result: DgcFetchResult) -> None:
        self.result = result
        self.parameters: Mapping[str, DgcTrialBalanceParameterValue] | None = None

    def fetch(
        self,
        parameters: Mapping[str, DgcTrialBalanceParameterValue],
    ) -> DgcFetchResult:
        self.parameters = parameters
        return self.result


class _FailingSource:
    def fetch(
        self,
        parameters: Mapping[str, DgcTrialBalanceParameterValue],
    ) -> DgcFetchResult:
        del parameters
        raise DgcTransportError("DGC data request failed at the transport layer")


class _Ingest:
    def __init__(self) -> None:
        self.batch = cast(BatchView, SimpleNamespace(id=uuid4()))
        self.metadata: BatchMetadata | None = None
        self.ingested: tuple[object, str, object] | None = None

    def create_batch(self, metadata: BatchMetadata) -> CreateBatchResult:
        self.metadata = metadata
        return CreateBatchResult(batch=self.batch, created=True)

    def ingest_adapter(self, batch_id: object, payload_ref: str, adapter: object) -> BatchView:
        self.ingested = (batch_id, payload_ref, adapter)
        return self.batch


def test_import_queries_one_annual_account_scope_and_persists_two_metrics() -> None:
    extracted_at = datetime(2026, 7, 1, 8, tzinfo=timezone.utc)
    source = _Source(
        DgcFetchResult(
            records=(
                _row(fiscal_period="03", total_debit_amount="900000"),
                _row(fiscal_period="06", total_debit_amount="700000"),
            ),
            checksum="a" * 64,
        )
    )
    ingest = _Ingest()
    service = DgcSapTrialBalanceImportService(cast(IngestService, ingest), source)

    result = service.import_current_income_tax(
        DgcSapTrialBalanceImportCommand(
            source_batch_key="sap-trial-balance-3000-2026-q2",
            extraction_time=extracted_at,
            company_code=" 3000 ",
            fiscal_year=" 2026 ",
            through_period=6,
            mode=IngestMode.FULL,
            schema_version=" 1 ",
            currency="cny",
            amount_scale=2,
        )
    )

    assert result.created is True
    assert source.parameters == {
        "company_code": "3000",
        "fiscal_year": "2026",
        "gl_account_code": CURRENT_INCOME_TAX_GL_ACCOUNT,
    }
    scope = {**source.parameters, "through_period": 6}
    scope_sha256 = dgc_trial_balance_scope_sha256(scope)
    assert ingest.metadata == BatchMetadata(
        source="DGC_SAP_TRIAL_BALANCE",
        source_batch_key="sap-trial-balance-3000-2026-q2",
        dataset_code="quarterly_metric",
        extraction_time=extracted_at,
        period=date(2026, 6, 30),
        mode=IngestMode.FULL,
        schema_version="1",
        currency="CNY",
        amount_scale=2,
        source_primary_key_definition={
            "fields": ["source_record_key"],
            "dgc_trial_balance_scope_sha256": scope_sha256,
        },
    )
    assert ingest.ingested is not None
    assert ingest.ingested[1] == f"dgc://sap-trial-balance?scope_sha256={scope_sha256}"
    adapter = cast(BulkFileAdapter, ingest.ingested[2])
    values = [
        row.value
        for row in adapter.iter_rows()
        if isinstance(row.value, CanonicalFinancialRow)
    ]
    assert [(value.metric_code, value.amount) for value in values] == [
        ("prior_quarter_current_tax", 900000),
        ("current_quarter_current_tax", 700000),
    ]


def test_import_persists_evidenced_zeros_when_company_has_not_accrued() -> None:
    source = _Source(DgcFetchResult(records=(), checksum="e" * 64))
    ingest = _Ingest()
    service = DgcSapTrialBalanceImportService(cast(IngestService, ingest), source)

    result = service.import_current_income_tax(_command())

    assert result.created is True
    assert ingest.ingested is not None
    adapter = cast(BulkFileAdapter, ingest.ingested[2])
    values = [
        row.value
        for row in adapter.iter_rows()
        if isinstance(row.value, CanonicalFinancialRow)
    ]
    assert [(value.metric_code, value.amount) for value in values] == [
        ("prior_quarter_current_tax", 0),
        ("current_quarter_current_tax", 0),
    ]
    assert adapter.checksum == "e" * 64


def test_remote_failure_does_not_create_a_partial_batch() -> None:
    ingest = _Ingest()
    service = DgcSapTrialBalanceImportService(
        cast(IngestService, ingest),
        _FailingSource(),
    )

    with pytest.raises(DgcTransportError):
        service.import_current_income_tax(_command())

    assert ingest.metadata is None
    assert ingest.ingested is None


def test_request_normalizes_company_year_and_quarter_end() -> None:
    request = DgcSapTrialBalanceImportRequest.model_validate(
        {
            "source_batch_key": " trial-3000-q2 ",
            "extraction_time": "2026-07-01T08:00:00Z",
            "company_code": " 3000 ",
            "fiscal_year": " 2026 ",
            "through_period": "06",
        }
    )

    assert request.source_batch_key == "trial-3000-q2"
    assert request.company_code == "3000"
    assert request.fiscal_year == "2026"
    assert request.through_period == 6


@pytest.mark.parametrize("through_period", [1, 4, 7, 10, 13, True, "Q2"])
def test_request_rejects_non_quarter_end_period(through_period: object) -> None:
    with pytest.raises(ValidationError, match="through_period"):
        DgcSapTrialBalanceImportRequest.model_validate(
            {
                "source_batch_key": "trial-3000-q2",
                "extraction_time": "2026-07-01T08:00:00Z",
                "company_code": "3000",
                "fiscal_year": "2026",
                "through_period": through_period,
            }
        )


def _command() -> DgcSapTrialBalanceImportCommand:
    return DgcSapTrialBalanceImportCommand(
        source_batch_key="sap-trial-balance-3000-2026-q2",
        extraction_time=datetime(2026, 7, 1, 8, tzinfo=timezone.utc),
        company_code="3000",
        fiscal_year="2026",
        through_period=6,
        mode=IngestMode.FULL,
        schema_version="1",
        currency="CNY",
        amount_scale=2,
    )


def _row(**overrides: object) -> dict[str, object]:
    return {
        "company_code": "3000",
        "company_name": "Company 3000",
        "fiscal_year": "2026",
        "fiscal_period": "06",
        "gl_account_code": CURRENT_INCOME_TAX_GL_ACCOUNT,
        "gl_account_name": "所得税费用-当期所得税费用",
        "bank_center_code": "",
        "bank_account_number": "",
        "cost_center_code": "",
        "cost_center_name": "",
        "profit_center_code": "",
        "profit_center_name": "",
        "internal_order_code": "",
        "internal_order_name": "",
        "business_area_code": "",
        "business_area_name": "",
        "customer_code": "",
        "customer_name": "",
        "vendor_code": "",
        "vendor_name": "",
        "asset_code": "",
        "asset_name": "",
        "rstgr": "",
        "rstgr_name": "",
        "input_tax_process_method": "",
        "sfkf": "",
        "total_debit_amount": "0",
        "total_credit_amount": "0",
    } | overrides
