from __future__ import annotations

from calendar import monthrange
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
import json
from typing import Protocol, TypeAlias

from tax_risk.adapters.ingest.dgc_sap_profit import (
    DgcFetchResult,
    DgcSapProfitAdapter,
    DgcSapProfitFieldMap,
    DgcSapProfitMetricMap,
)
from tax_risk.application.ingest import BatchMetadata, BatchView, IngestService
from tax_risk.persistence.ingest_models import IngestMode


DgcParameterValue: TypeAlias = str | int | float | bool | None


class DgcSapProfitSource(Protocol):
    def fetch(self, parameters: Mapping[str, DgcParameterValue]) -> DgcFetchResult: ...


@dataclass(frozen=True, slots=True)
class DgcSapProfitImportCommand:
    source_batch_key: str
    extraction_time: datetime
    gjahr: str
    monat: str
    bukrs: str | None
    mode: IngestMode
    schema_version: str
    currency: str
    amount_scale: int


@dataclass(frozen=True, slots=True)
class DgcSapProfitImportResult:
    batch: BatchView
    created: bool


class DgcSapProfitImportService:
    """Pull one DGC profit-statement slice and ingest it as quarterly metrics."""

    SOURCE = "DGC_SAP"
    DATASET_CODE = "quarterly_metric"
    PAYLOAD_REF = "dgc://sap-income"

    def __init__(
        self,
        ingest_service: IngestService,
        source: DgcSapProfitSource,
        field_map: DgcSapProfitFieldMap,
        metric_map: DgcSapProfitMetricMap,
        ledger: str,
    ) -> None:
        self._ingest_service = ingest_service
        self._source = source
        self._field_map = field_map
        self._metric_map = metric_map
        self._ledger = ledger

    def import_profit_statement(
        self,
        command: DgcSapProfitImportCommand,
    ) -> DgcSapProfitImportResult:
        gjahr, monat, period = normalize_sap_period(command.gjahr, command.monat)
        bukrs = command.bukrs.strip() if command.bukrs is not None else None
        if bukrs == "":
            raise ValueError("bukrs must be a non-empty string when provided")
        parameters: dict[str, DgcParameterValue] = {
            "gjahr": gjahr,
            "monat": monat,
        }
        if bukrs is not None:
            parameters["bukrs"] = bukrs
        parameters_sha256 = dgc_parameters_sha256(parameters)
        # Complete the remote read before opening the ingest transaction.
        fetched = self._source.fetch(parameters)
        adapter = DgcSapProfitAdapter(
            fetched,
            field_map=self._field_map,
            metric_map=self._metric_map,
            ledger=self._ledger,
            expected_company_code=bukrs,
            currency=command.currency,
            amount_scale=command.amount_scale,
            extracted_at=command.extraction_time,
        )
        created = self._ingest_service.create_batch(
            BatchMetadata(
                source=self.SOURCE,
                source_batch_key=command.source_batch_key,
                dataset_code=self.DATASET_CODE,
                extraction_time=command.extraction_time,
                period=period,
                mode=command.mode,
                schema_version=command.schema_version,
                currency=command.currency,
                amount_scale=command.amount_scale,
                source_primary_key_definition={
                    "fields": ["source_record_key"],
                    "dgc_parameters_sha256": parameters_sha256,
                },
            )
        )
        batch = self._ingest_service.ingest_adapter(
            created.batch.id,
            f"{self.PAYLOAD_REF}?query_sha256={parameters_sha256}",
            adapter,
        )
        return DgcSapProfitImportResult(batch=batch, created=created.created)


def normalize_sap_period(gjahr: str, monat: str) -> tuple[str, str, date]:
    if (
        not isinstance(gjahr, str)
        or not gjahr.strip().isascii()
        or not gjahr.strip().isdigit()
        or len(gjahr.strip()) != 4
    ):
        raise ValueError("gjahr must be a four-digit year")
    if (
        not isinstance(monat, str)
        or not monat.strip().isascii()
        or not monat.strip().isdigit()
    ):
        raise ValueError("monat must be a month from 01 to 12")
    fiscal_year = int(gjahr.strip())
    fiscal_period = int(monat.strip())
    if fiscal_year < 2000 or fiscal_year > 9999:
        raise ValueError("gjahr must be between 2000 and 9999")
    if fiscal_period < 1 or fiscal_period > 12:
        raise ValueError("monat must be a month from 01 to 12")
    normalized_year = f"{fiscal_year:04d}"
    normalized_month = f"{fiscal_period:02d}"
    period = date(
        fiscal_year,
        fiscal_period,
        monthrange(fiscal_year, fiscal_period)[1],
    )
    return normalized_year, normalized_month, period


def dgc_parameters_sha256(parameters: Mapping[str, DgcParameterValue]) -> str:
    encoded = json.dumps(
        dict(parameters),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


__all__ = [
    "DgcParameterValue",
    "DgcSapProfitImportCommand",
    "DgcSapProfitImportResult",
    "DgcSapProfitImportService",
    "DgcSapProfitSource",
    "dgc_parameters_sha256",
    "normalize_sap_period",
]
