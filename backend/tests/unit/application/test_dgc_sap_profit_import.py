from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import cast
from uuid import uuid4

import pytest
from pydantic import ValidationError

from tax_risk.adapters.ingest.dgc_sap_profit import (
    DgcFetchResult,
    DgcSapProfitFieldMap,
    DgcSapProfitMetricMap,
    DgcTransportError,
)
from tax_risk.adapters.ingest.base import BulkFileAdapter, CanonicalFinancialRow
from tax_risk.api.schemas import DgcSapProfitImportRequest
from tax_risk.application.dgc_sap_profit import (
    DgcParameterValue,
    DgcSapProfitImportCommand,
    DgcSapProfitImportService,
    dgc_parameters_sha256,
    normalize_sap_period,
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
        self.parameters: Mapping[str, DgcParameterValue] | None = None

    def fetch(self, parameters: Mapping[str, DgcParameterValue]) -> DgcFetchResult:
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


class _FailingSource:
    def fetch(self, parameters: Mapping[str, DgcParameterValue]) -> DgcFetchResult:
        del parameters
        raise DgcTransportError("DGC data request failed at the transport layer")


def test_import_pulls_before_building_controlled_quarterly_batch() -> None:
    extracted_at = datetime(2026, 4, 1, 8, tzinfo=timezone.utc)
    source = _Source(
        DgcFetchResult(
            records=(
                {
                    "mandt": "100",
                    "bukrs": "C001",
                    "companyname": "Company One",
                    "gjahr": "2026",
                    "monat": "03",
                    "rldnr": "0L",
                    "hs": "10",
                    "ztext": "四、利润总额",
                    "nmhsl": "10.00",
                    "nyhsl": "100.00",
                },
            ),
            checksum="a" * 64,
        )
    )
    ingest = _Ingest()
    service = DgcSapProfitImportService(
        cast(IngestService, ingest),
        source,
        DgcSapProfitFieldMap(),
        DgcSapProfitMetricMap(),
        "0L",
    )

    result = service.import_profit_statement(
        DgcSapProfitImportCommand(
            source_batch_key="sap-profit-2026-q1",
            extraction_time=extracted_at,
            gjahr="2026",
            monat="3",
            bukrs="C001",
            mode=IngestMode.FULL,
            schema_version="1",
            currency="CNY",
            amount_scale=2,
        )
    )

    assert result.created is True
    assert result.batch is ingest.batch
    expected_parameters = {"gjahr": "2026", "monat": "03", "bukrs": "C001"}
    assert source.parameters == expected_parameters
    parameters_sha256 = dgc_parameters_sha256(expected_parameters)
    assert ingest.metadata == BatchMetadata(
        source="DGC_SAP",
        source_batch_key="sap-profit-2026-q1",
        dataset_code="quarterly_metric",
        extraction_time=extracted_at,
        period=date(2026, 3, 31),
        mode=IngestMode.FULL,
        schema_version="1",
        currency="CNY",
        amount_scale=2,
        source_primary_key_definition={
            "fields": ["source_record_key"],
            "dgc_parameters_sha256": parameters_sha256,
        },
    )
    assert ingest.ingested is not None
    assert ingest.ingested[0] == ingest.batch.id
    assert ingest.ingested[1] == f"dgc://sap-income?query_sha256={parameters_sha256}"
    adapter = cast(BulkFileAdapter, ingest.ingested[2])
    rows = list(adapter.iter_rows())
    values = [cast(CanonicalFinancialRow, row.value) for row in rows if row.value is not None]
    assert [value.metric_code for value in values] == ["cumulative_profit"]


def test_remote_failure_does_not_create_a_partial_ingest_batch() -> None:
    ingest = _Ingest()
    service = DgcSapProfitImportService(
        cast(IngestService, ingest),
        _FailingSource(),
        DgcSapProfitFieldMap(),
        DgcSapProfitMetricMap(),
        "0L",
    )

    with pytest.raises(DgcTransportError):
        service.import_profit_statement(
            DgcSapProfitImportCommand(
                source_batch_key="sap-profit-2026-q1",
                extraction_time=datetime(2026, 4, 1, 8, tzinfo=timezone.utc),
                gjahr="2026",
                monat="03",
                bukrs=None,
                mode=IngestMode.FULL,
                schema_version="1",
                currency="CNY",
                amount_scale=2,
            )
        )

    assert ingest.metadata is None
    assert ingest.ingested is None


def test_dgc_parameter_hash_is_order_stable_and_query_sensitive() -> None:
    first = dgc_parameters_sha256({"gjahr": "2026", "monat": "03", "bukrs": "C001"})
    reordered = dgc_parameters_sha256({"bukrs": "C001", "monat": "03", "gjahr": "2026"})
    different = dgc_parameters_sha256({"gjahr": "2026", "monat": "03", "bukrs": "C002"})

    assert first == reordered
    assert first != different


def test_import_request_normalizes_real_sap_parameters() -> None:
    request = DgcSapProfitImportRequest.model_validate(
        {
            "source_batch_key": "sap-profit-2026-q1",
            "extraction_time": "2026-04-01T08:00:00Z",
            "gjahr": " 2026 ",
            "monat": "3",
            "bukrs": " C001 ",
        }
    )

    assert request.gjahr == "2026"
    assert request.monat == "03"
    assert request.bukrs == "C001"
    assert normalize_sap_period(request.gjahr, request.monat) == (
        "2026",
        "03",
        date(2026, 3, 31),
    )


@pytest.mark.parametrize("monat", ["00", "13", "001", 3])
def test_import_request_rejects_invalid_sap_period(monat: object) -> None:
    with pytest.raises(ValidationError, match="monat"):
        DgcSapProfitImportRequest.model_validate(
            {
                "source_batch_key": "sap-profit-2026-q1",
                "extraction_time": "2026-04-01T08:00:00Z",
                "gjahr": "2026",
                "monat": monat,
            }
        )


def test_import_request_rejects_old_arbitrary_parameters_contract() -> None:
    with pytest.raises(ValidationError, match="extra"):
        DgcSapProfitImportRequest.model_validate(
            {
                "source_batch_key": "sap-profit-2026-q1",
                "extraction_time": "2026-04-01T08:00:00Z",
                "gjahr": "2026",
                "monat": "03",
                "parameters": {"limitValue": 100},
            }
        )


def test_import_request_requires_timezone() -> None:
    with pytest.raises(ValidationError, match="UTC offset"):
        DgcSapProfitImportRequest.model_validate(
            {
                "source_batch_key": "sap-profit-2026-q1",
                "extraction_time": "2026-04-01T08:00:00",
                "gjahr": "2026",
                "monat": "03",
            }
        )
