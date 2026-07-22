"""Query and controlled-ingest contracts for 汇算清缴相关科目明细."""

from __future__ import annotations

from calendar import monthrange
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
import json
from typing import Protocol, TypeAlias

from tax_risk.adapters.ingest.dgc_sap_dividend_detail import (
    DgcSapDividendDetailAdapter,
    DgcSapDividendDetailResult,
    DgcSapDividendMetricAdapter,
)
from tax_risk.adapters.ingest.dgc_sap_profit import DgcFetchResult
from tax_risk.application.ingest import BatchMetadata, BatchView, IngestService
from tax_risk.persistence.ingest_models import IngestMode


DgcDividendParameterValue: TypeAlias = str | int | float | bool | None


class DgcSapDividendDetailSource(Protocol):
    def fetch(
        self,
        parameters: Mapping[str, DgcDividendParameterValue],
    ) -> DgcFetchResult: ...


@dataclass(frozen=True, slots=True)
class DgcSapDividendDetailQuery:
    company: str
    fiscal_year: str
    through_period: int = 12


@dataclass(frozen=True, slots=True)
class DgcSapDividendDetailImportCommand:
    source_batch_key: str
    extraction_time: datetime
    company: str
    fiscal_year: str
    through_period: int
    mode: IngestMode
    schema_version: str
    currency: str
    amount_scale: int


@dataclass(frozen=True, slots=True)
class DgcSapDividendDetailImportResult:
    batch: BatchView
    created: bool


class DgcSapDividendDetailQueryService:
    """Fetch and calculate one company's annual cumulative dividend."""

    def __init__(self, source: DgcSapDividendDetailSource) -> None:
        self._source = source

    def query(
        self,
        request: DgcSapDividendDetailQuery,
    ) -> DgcSapDividendDetailResult:
        company = _normalize_company(request.company)
        fiscal_year = _normalize_fiscal_year(request.fiscal_year)
        through_period = _normalize_through_period(request.through_period)
        parameters: dict[str, DgcDividendParameterValue] = {
            "company": company,
            "fiscal_year": fiscal_year,
        }
        fetched = self._source.fetch(parameters)
        return DgcSapDividendDetailAdapter(
            fetched,
            expected_company=company,
            expected_fiscal_year=fiscal_year,
            through_period=through_period,
        ).adapt()


class DgcSapDividendDetailImportService:
    """Pull one company-year scope and persist its cumulative dividend metric."""

    SOURCE = "DGC_SAP_DIVIDEND"
    DATASET_CODE = "quarterly_metric"
    PAYLOAD_REF = "dgc://sap-dividend-detail"

    def __init__(
        self,
        ingest_service: IngestService,
        source: DgcSapDividendDetailSource,
    ) -> None:
        self._ingest_service = ingest_service
        self._source = source

    def import_dividend_detail(
        self,
        command: DgcSapDividendDetailImportCommand,
    ) -> DgcSapDividendDetailImportResult:
        source_batch_key = _normalize_nonempty_text(
            command.source_batch_key,
            "source_batch_key",
        )
        schema_version = _normalize_nonempty_text(
            command.schema_version,
            "schema_version",
        )
        company = _normalize_company(command.company)
        fiscal_year = _normalize_fiscal_year(command.fiscal_year)
        through_period = _normalize_quarterly_through_period(command.through_period)
        currency = _normalize_currency(command.currency)
        amount_scale = _normalize_amount_scale(command.amount_scale)
        extraction_time = _normalize_extraction_time(command.extraction_time)
        if not isinstance(command.mode, IngestMode):
            raise TypeError("mode must be an IngestMode")

        scope = {
            "company": company,
            "fiscal_year": fiscal_year,
            "through_period": through_period,
        }
        scope_sha256 = dgc_dividend_scope_sha256(scope)
        # The complete remote read, strict response validation, and canonical row
        # materialization all happen before the ingest transaction is opened.
        result = DgcSapDividendDetailQueryService(self._source).query(
            DgcSapDividendDetailQuery(
                company=company,
                fiscal_year=fiscal_year,
                through_period=through_period,
            )
        )
        adapter = DgcSapDividendMetricAdapter(
            result,
            company_code=company,
            fiscal_year=int(fiscal_year),
            through_period=through_period,
            currency=currency,
            amount_scale=amount_scale,
            extracted_at=extraction_time,
        )
        period = date(
            int(fiscal_year),
            through_period,
            monthrange(int(fiscal_year), through_period)[1],
        )
        created = self._ingest_service.create_batch(
            BatchMetadata(
                source=self.SOURCE,
                source_batch_key=source_batch_key,
                dataset_code=self.DATASET_CODE,
                extraction_time=extraction_time,
                period=period,
                mode=command.mode,
                schema_version=schema_version,
                currency=currency,
                amount_scale=amount_scale,
                source_primary_key_definition={
                    "fields": ["source_record_key"],
                    "dgc_dividend_scope_sha256": scope_sha256,
                },
            )
        )
        batch = self._ingest_service.ingest_adapter(
            created.batch.id,
            f"{self.PAYLOAD_REF}?scope_sha256={scope_sha256}",
            adapter,
        )
        return DgcSapDividendDetailImportResult(
            batch=batch,
            created=created.created,
        )


def _normalize_company(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("company must be a non-empty string")
    return value.strip()


def _normalize_fiscal_year(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("fiscal_year must be a four-digit year")
    normalized = value.strip()
    if len(normalized) != 4 or not normalized.isascii() or not normalized.isdecimal():
        raise ValueError("fiscal_year must be a four-digit year")
    if not 2000 <= int(normalized) <= 9999:
        raise ValueError("fiscal_year must be between 2000 and 9999")
    return normalized


def _normalize_through_period(value: object) -> int:
    if type(value) is not int or not 1 <= value <= 12:
        raise ValueError("through_period must be an integer from 1 to 12")
    return value


def _normalize_quarterly_through_period(value: object) -> int:
    normalized = _normalize_through_period(value)
    if normalized not in {3, 6, 9, 12}:
        raise ValueError("through_period must be a quarter-end month: 3, 6, 9, or 12")
    return normalized


def _normalize_nonempty_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _normalize_currency(value: object) -> str:
    if not isinstance(value, str):
        raise TypeError("currency must be a string")
    normalized = value.strip().upper()
    if len(normalized) != 3 or not normalized.isascii() or not normalized.isalpha():
        raise ValueError("currency must be three ASCII letters")
    return normalized


def _normalize_amount_scale(value: object) -> int:
    if type(value) is not int:
        raise TypeError("amount_scale must be an integer")
    if not 0 <= value <= 12:
        raise ValueError("amount_scale must be between 0 and 12")
    return value


def _normalize_extraction_time(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("extraction_time must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("extraction_time must include a UTC offset")
    return value


def dgc_dividend_scope_sha256(
    scope: Mapping[str, object],
) -> str:
    encoded = json.dumps(
        dict(scope),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


__all__ = [
    "DgcDividendParameterValue",
    "DgcSapDividendDetailImportCommand",
    "DgcSapDividendDetailImportResult",
    "DgcSapDividendDetailImportService",
    "DgcSapDividendDetailQuery",
    "DgcSapDividendDetailQueryService",
    "DgcSapDividendDetailSource",
    "dgc_dividend_scope_sha256",
]
