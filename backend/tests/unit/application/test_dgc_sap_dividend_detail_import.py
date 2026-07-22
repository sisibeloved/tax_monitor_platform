from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timezone
from decimal import Decimal
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from tax_risk.adapters.ingest.base import BulkFileAdapter, CanonicalFinancialRow
from tax_risk.adapters.ingest.dgc_sap_dividend_detail import DgcSapDividendDetailError
from tax_risk.adapters.ingest.dgc_sap_profit import DgcFetchResult, DgcTransportError
from tax_risk.api.schemas import DgcSapDividendDetailImportRequest
from tax_risk.application.dgc_sap_dividend_detail import (
    DgcDividendParameterValue,
    DgcSapDividendDetailImportCommand,
    DgcSapDividendDetailImportService,
    dgc_dividend_scope_sha256,
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
        self.parameters: Mapping[str, DgcDividendParameterValue] | None = None

    def fetch(
        self,
        parameters: Mapping[str, DgcDividendParameterValue],
    ) -> DgcFetchResult:
        self.parameters = dict(parameters)
        return self.result


class _FailingSource:
    def __init__(self) -> None:
        self.called = False

    def fetch(
        self,
        parameters: Mapping[str, DgcDividendParameterValue],
    ) -> DgcFetchResult:
        del parameters
        self.called = True
        raise DgcTransportError("DGC data request failed at the transport layer")


class _Ingest:
    def __init__(self) -> None:
        self.batch = cast(BatchView, SimpleNamespace(id=uuid4()))
        self.metadata: BatchMetadata | None = None
        self.ingested: tuple[UUID, str, BulkFileAdapter] | None = None

    def create_batch(self, metadata: BatchMetadata) -> CreateBatchResult:
        self.metadata = metadata
        return CreateBatchResult(batch=self.batch, created=True)

    def ingest_adapter(
        self,
        batch_id: UUID,
        payload_ref: str,
        adapter: BulkFileAdapter,
    ) -> BatchView:
        self.ingested = (batch_id, payload_ref, adapter)
        return self.batch


def test_import_pulls_and_validates_before_building_controlled_quarterly_metric() -> None:
    source = _Source(
        DgcFetchResult(
            records=(
                _row(
                    voucher_no="q2",
                    fiscal_period="006",
                    header_text="收到分红",
                    amount_ksl="-12.50",
                ),
                _row(
                    voucher_no="future-q3",
                    fiscal_period="007",
                    header_text="收到分红",
                    amount_ksl="-20.00",
                ),
            ),
            checksum="b" * 64,
        )
    )
    ingest = _Ingest()
    service = DgcSapDividendDetailImportService(cast(IngestService, ingest), source)
    extracted_at = datetime(2026, 7, 1, 8, tzinfo=timezone.utc)

    result = service.import_dividend_detail(_command(extraction_time=extracted_at))

    assert result.created is True
    assert result.batch is ingest.batch
    assert source.parameters == {"company": "3730", "fiscal_year": "2026"}
    scope = {"company": "3730", "fiscal_year": "2026", "through_period": 6}
    scope_sha256 = dgc_dividend_scope_sha256(scope)
    assert ingest.metadata == BatchMetadata(
        source="DGC_SAP_DIVIDEND",
        source_batch_key="sap-dividend-3730-2026-q2",
        dataset_code="quarterly_metric",
        extraction_time=extracted_at,
        period=date(2026, 6, 30),
        mode=IngestMode.FULL,
        schema_version="1",
        currency="CNY",
        amount_scale=2,
        source_primary_key_definition={
            "fields": ["source_record_key"],
            "dgc_dividend_scope_sha256": scope_sha256,
        },
    )
    assert ingest.ingested is not None
    assert ingest.ingested[0] == ingest.batch.id
    assert ingest.ingested[1] == (f"dgc://sap-dividend-detail?scope_sha256={scope_sha256}")
    adapter = ingest.ingested[2]
    assert adapter.checksum == "b" * 64
    rows = list(adapter.iter_rows())
    assert len(rows) == 1
    value = rows[0].value
    assert isinstance(value, CanonicalFinancialRow)
    assert value.metric_code == "received_dividends"
    assert value.amount == Decimal("12.50")
    assert value.period == date(2026, 6, 30)


def test_import_outputs_evidenced_zero_in_command_currency_when_nothing_matches() -> None:
    source = _Source(
        DgcFetchResult(
            records=(_row(header_text="ordinary investment income"),),
            checksum="c" * 64,
        )
    )
    ingest = _Ingest()

    DgcSapDividendDetailImportService(
        cast(IngestService, ingest),
        source,
    ).import_dividend_detail(_command(currency=" cny "))

    assert ingest.ingested is not None
    value = list(ingest.ingested[2].iter_rows())[0].value
    assert isinstance(value, CanonicalFinancialRow)
    assert value.amount == Decimal(0)
    assert value.currency == "CNY"


def test_remote_failure_does_not_create_a_partial_ingest_batch() -> None:
    source = _FailingSource()
    ingest = _Ingest()

    with pytest.raises(DgcTransportError):
        DgcSapDividendDetailImportService(
            cast(IngestService, ingest),
            source,
        ).import_dividend_detail(_command())

    assert source.called is True
    assert ingest.metadata is None
    assert ingest.ingested is None


def test_response_currency_mismatch_does_not_create_a_partial_ingest_batch() -> None:
    source = _Source(
        DgcFetchResult(
            records=(_row(header_text="分红", amount_ksl="-1", group_currency="USD"),),
            checksum="d" * 64,
        )
    )
    ingest = _Ingest()

    with pytest.raises(DgcSapDividendDetailError) as raised:
        DgcSapDividendDetailImportService(
            cast(IngestService, ingest),
            source,
        ).import_dividend_detail(_command(currency="CNY"))

    assert raised.value.error_code == "DGC_RESPONSE_CURRENCY_MISMATCH"
    assert ingest.metadata is None
    assert ingest.ingested is None


@pytest.mark.parametrize(
    "override",
    [
        {"company": ""},
        {"fiscal_year": "1999"},
        {"through_period": 0},
        {"through_period": 5},
        {"through_period": 13},
        {"currency": "CN"},
        {"amount_scale": 13},
        {"extraction_time": datetime(2026, 7, 1, 8)},
    ],
)
def test_invalid_command_is_rejected_before_remote_read(override: dict[str, object]) -> None:
    source = _Source(DgcFetchResult(records=(), checksum="e" * 64))
    ingest = _Ingest()

    with pytest.raises((TypeError, ValueError)):
        DgcSapDividendDetailImportService(
            cast(IngestService, ingest),
            source,
        ).import_dividend_detail(_command(**override))

    assert source.parameters is None
    assert ingest.metadata is None


def test_scope_hash_is_order_stable_and_period_sensitive() -> None:
    first = dgc_dividend_scope_sha256(
        {"company": "3730", "fiscal_year": "2026", "through_period": 6}
    )
    reordered = dgc_dividend_scope_sha256(
        {"through_period": 6, "fiscal_year": "2026", "company": "3730"}
    )
    later_period = dgc_dividend_scope_sha256(
        {"company": "3730", "fiscal_year": "2026", "through_period": 9}
    )

    assert first == reordered
    assert first != later_period


def test_import_request_normalizes_company_year_and_quarter_end() -> None:
    request = DgcSapDividendDetailImportRequest.model_validate(
        {
            "source_batch_key": " dividend-q2 ",
            "extraction_time": "2026-07-01T08:00:00Z",
            "company": " 3730 ",
            "fiscal_year": " 2026 ",
            "through_period": "006",
        }
    )

    assert request.source_batch_key == "dividend-q2"
    assert request.company == "3730"
    assert request.fiscal_year == "2026"
    assert request.through_period == 6


@pytest.mark.parametrize("through_period", [0, 1, 4, 10, 13, True, "Q2"])
def test_import_request_rejects_non_quarter_end_period(
    through_period: object,
) -> None:
    with pytest.raises(ValidationError, match="through_period"):
        DgcSapDividendDetailImportRequest.model_validate(
            {
                "source_batch_key": "dividend-q2",
                "extraction_time": "2026-07-01T08:00:00Z",
                "company": "3730",
                "fiscal_year": "2026",
                "through_period": through_period,
            }
        )


def test_import_request_rejects_extra_remote_pagination_parameters() -> None:
    with pytest.raises(ValidationError, match="extra"):
        DgcSapDividendDetailImportRequest.model_validate(
            {
                "source_batch_key": "dividend-q2",
                "extraction_time": "2026-07-01T08:00:00Z",
                "company": "3730",
                "fiscal_year": "2026",
                "through_period": 6,
                "limitValue": 1,
            }
        )


def _command(**overrides: object) -> DgcSapDividendDetailImportCommand:
    values: dict[str, object] = {
        "source_batch_key": "sap-dividend-3730-2026-q2",
        "extraction_time": datetime(2026, 7, 1, 8, tzinfo=timezone.utc),
        "company": "3730",
        "fiscal_year": "2026",
        "through_period": 6,
        "mode": IngestMode.FULL,
        "schema_version": "1",
        "currency": "CNY",
        "amount_scale": 2,
    }
    values.update(overrides)
    return DgcSapDividendDetailImportCommand(**values)  # type: ignore[arg-type]


def _row(**overrides: object) -> dict[str, object]:
    return {
        "company": "3730",
        "companyname": "Company 3730",
        "fiscal_year": "2026",
        "fiscal_period": "006",
        "voucher_no": "100000",
        "header_text": "",
        "detail_text": "",
        "amount_ksl": "0",
        "gl_account": "6111010000",
        "account_name": "投资收益",
        "project_code": "",
        "project_name": "",
        "debit_credit_flag": "H",
        "group_currency": "CNY",
        "original_system_doc_no": "",
    } | overrides
