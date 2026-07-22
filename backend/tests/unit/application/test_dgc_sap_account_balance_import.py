from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

from pydantic import ValidationError
import pytest

from tax_risk.adapters.ingest.base import BulkFileAdapter, CanonicalFinancialRow
from tax_risk.adapters.ingest.dgc_sap_profit import DgcFetchResult
from tax_risk.api.schemas import DgcSapAccountBalanceImportRequest
from tax_risk.application.dgc_sap_account_balance import (
    DgcAccountBalanceParameterValue,
    DgcSapAccountBalanceImportCommand,
    DgcSapAccountBalanceImportService,
    dgc_account_balance_scope_sha256,
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
        self.parameters: Mapping[str, DgcAccountBalanceParameterValue] | None = None

    def fetch(
        self,
        parameters: Mapping[str, DgcAccountBalanceParameterValue],
    ) -> DgcFetchResult:
        self.parameters = parameters
        return self.result


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


def test_import_uses_three_digit_period_and_persists_supported_metrics() -> None:
    extracted_at = datetime(2026, 7, 1, 8, tzinfo=timezone.utc)
    source = _Source(
        DgcFetchResult(
            records=(
                _row("2241050900", "-25"),
                _row("1811030000", "80"),
            ),
            checksum="a" * 64,
        )
    )
    ingest = _Ingest()
    service = DgcSapAccountBalanceImportService(cast(IngestService, ingest), source)

    result = service.import_quarterly_balances(
        DgcSapAccountBalanceImportCommand(
            source_batch_key="sap-account-balance-3000-2026-q2",
            extraction_time=extracted_at,
            company_code=" 3000 ",
            fiscal_year=" 2026 ",
            fiscal_period=6,
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
        "fiscal_period": "006",
    }
    scope_sha256 = dgc_account_balance_scope_sha256(source.parameters)
    assert ingest.metadata == BatchMetadata(
        source="DGC_SAP_ACCOUNT_BALANCE",
        source_batch_key="sap-account-balance-3000-2026-q2",
        dataset_code="quarterly_metric",
        extraction_time=extracted_at,
        period=date(2026, 6, 30),
        mode=IngestMode.FULL,
        schema_version="1",
        currency="CNY",
        amount_scale=2,
        source_primary_key_definition={
            "fields": ["source_record_key"],
            "dgc_account_balance_scope_sha256": scope_sha256,
        },
    )
    assert ingest.ingested is not None
    assert ingest.ingested[1] == f"dgc://sap-account-balance?scope_sha256={scope_sha256}"
    adapter = cast(BulkFileAdapter, ingest.ingested[2])
    metrics = {
        row.value.metric_code: row.value.amount
        for row in adapter.iter_rows()
        if isinstance(row.value, CanonicalFinancialRow)
    }
    assert metrics == {
        "other_payables_accrual": 25,
        "sap_cumulative_deferred_tax_expense": 80,
    }


def test_import_materializes_zero_when_deferred_tax_account_is_absent() -> None:
    source = _Source(
        DgcFetchResult(records=(_row("1001000000", "1"),), checksum="b" * 64)
    )
    ingest = _Ingest()
    service = DgcSapAccountBalanceImportService(cast(IngestService, ingest), source)

    service.import_quarterly_balances(_command())

    assert ingest.ingested is not None
    adapter = cast(BulkFileAdapter, ingest.ingested[2])
    metrics = {
        row.value.metric_code: row.value.amount
        for row in adapter.iter_rows()
        if isinstance(row.value, CanonicalFinancialRow)
    }
    assert metrics == {"sap_cumulative_deferred_tax_expense": Decimal(0)}


def test_request_normalizes_company_year_and_quarter_end() -> None:
    request = DgcSapAccountBalanceImportRequest.model_validate(
        {
            "source_batch_key": " account-3000-q2 ",
            "extraction_time": "2026-07-01T08:00:00Z",
            "company_code": " 3000 ",
            "fiscal_year": " 2026 ",
            "fiscal_period": "006",
        }
    )

    assert request.source_batch_key == "account-3000-q2"
    assert request.company_code == "3000"
    assert request.fiscal_year == "2026"
    assert request.fiscal_period == 6


@pytest.mark.parametrize("fiscal_period", [1, 4, 7, 10, 13, True, "Q2"])
def test_request_rejects_non_quarter_end_period(fiscal_period: object) -> None:
    with pytest.raises(ValidationError, match="fiscal_period"):
        DgcSapAccountBalanceImportRequest.model_validate(
            {
                "source_batch_key": "account-3000-q2",
                "extraction_time": "2026-07-01T08:00:00Z",
                "company_code": "3000",
                "fiscal_year": "2026",
                "fiscal_period": fiscal_period,
            }
        )


def _command() -> DgcSapAccountBalanceImportCommand:
    return DgcSapAccountBalanceImportCommand(
        source_batch_key="sap-account-balance-3000-2026-q2",
        extraction_time=datetime(2026, 7, 1, 8, tzinfo=timezone.utc),
        company_code="3000",
        fiscal_year="2026",
        fiscal_period=6,
        mode=IngestMode.FULL,
        schema_version="1",
        currency="CNY",
        amount_scale=2,
    )


def _row(account_code: str, closing_balance: object) -> dict[str, object]:
    return {
        "account_code": account_code,
        "account_name": f"Account {account_code}",
        "closing_balance": closing_balance,
        "company_code": "3000",
        "company_name": "Company 3000",
        "credit_amount": "0",
        "debit_amount": "0",
        "fiscal_period": "006",
        "fiscal_year": "2026",
        "input_tax_process_method": "",
        "net_amount": "0",
        "opening_balance": "0",
        "sfkf": "",
    }
