"""Controlled ingest for quarterly current-income-tax amounts from SAP."""

from __future__ import annotations

from calendar import monthrange
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
import json
from typing import Protocol, TypeAlias

from tax_risk.adapters.ingest.dgc_sap_profit import DgcFetchResult
from tax_risk.adapters.ingest.dgc_sap_trial_balance import (
    CURRENT_INCOME_TAX_GL_ACCOUNT,
    DgcSapTrialBalanceAdapter,
    DgcSapTrialBalanceMetricAdapter,
)
from tax_risk.application.ingest import BatchMetadata, BatchView, IngestService
from tax_risk.persistence.ingest_models import IngestMode


DgcTrialBalanceParameterValue: TypeAlias = str | int | float | bool | None


class DgcSapTrialBalanceSource(Protocol):
    def fetch(
        self,
        parameters: Mapping[str, DgcTrialBalanceParameterValue],
    ) -> DgcFetchResult: ...


@dataclass(frozen=True, slots=True)
class DgcSapTrialBalanceImportCommand:
    source_batch_key: str
    extraction_time: datetime
    company_code: str
    fiscal_year: str
    through_period: int
    mode: IngestMode
    schema_version: str
    currency: str
    amount_scale: int


@dataclass(frozen=True, slots=True)
class DgcSapTrialBalanceImportResult:
    batch: BatchView
    created: bool


class DgcSapTrialBalanceImportService:
    """Pull one company-year account scope and persist two quarterly metrics."""

    SOURCE = "DGC_SAP_TRIAL_BALANCE"
    DATASET_CODE = "quarterly_metric"
    PAYLOAD_REF = "dgc://sap-trial-balance"

    def __init__(
        self,
        ingest_service: IngestService,
        source: DgcSapTrialBalanceSource,
    ) -> None:
        self._ingest_service = ingest_service
        self._source = source

    def import_current_income_tax(
        self,
        command: DgcSapTrialBalanceImportCommand,
    ) -> DgcSapTrialBalanceImportResult:
        source_batch_key = _nonempty(command.source_batch_key, "source_batch_key")
        schema_version = _nonempty(command.schema_version, "schema_version")
        company_code = _nonempty(command.company_code, "company_code")
        fiscal_year = _fiscal_year(command.fiscal_year)
        through_period = _quarter_end(command.through_period)
        currency = _currency(command.currency)
        amount_scale = _amount_scale(command.amount_scale)
        extraction_time = _extraction_time(command.extraction_time)
        if not isinstance(command.mode, IngestMode):
            raise TypeError("mode must be an IngestMode")

        parameters: dict[str, DgcTrialBalanceParameterValue] = {
            "company_code": company_code,
            "fiscal_year": fiscal_year,
            "gl_account_code": CURRENT_INCOME_TAX_GL_ACCOUNT,
        }
        scope = {**parameters, "through_period": through_period}
        scope_sha256 = dgc_trial_balance_scope_sha256(scope)
        # Fetch and aggregate before opening an ingest transaction. One annual
        # query is partitioned locally so the API is not called once per month.
        fetched = self._source.fetch(parameters)
        result = DgcSapTrialBalanceAdapter(
            fetched,
            expected_company_code=company_code,
            expected_fiscal_year=fiscal_year,
            through_period=through_period,
        ).adapt()
        adapter = DgcSapTrialBalanceMetricAdapter(
            result,
            company_code=company_code,
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
                    "dgc_trial_balance_scope_sha256": scope_sha256,
                },
            )
        )
        batch = self._ingest_service.ingest_adapter(
            created.batch.id,
            f"{self.PAYLOAD_REF}?scope_sha256={scope_sha256}",
            adapter,
        )
        return DgcSapTrialBalanceImportResult(batch=batch, created=created.created)


def dgc_trial_balance_scope_sha256(scope: Mapping[str, object]) -> str:
    encoded = json.dumps(
        dict(scope),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _fiscal_year(value: object) -> str:
    normalized = _nonempty(value, "fiscal_year")
    if (
        len(normalized) != 4
        or not normalized.isascii()
        or not normalized.isdecimal()
        or not 2000 <= int(normalized) <= 9999
    ):
        raise ValueError("fiscal_year must be a four-digit year from 2000 to 9999")
    return normalized


def _quarter_end(value: object) -> int:
    if type(value) is not int or value not in {3, 6, 9, 12}:
        raise ValueError("through_period must be one of 3, 6, 9, or 12")
    return value


def _currency(value: object) -> str:
    normalized = _nonempty(value, "currency").upper()
    if len(normalized) != 3 or not normalized.isascii() or not normalized.isalpha():
        raise ValueError("currency must be three ASCII letters")
    return normalized


def _amount_scale(value: object) -> int:
    if type(value) is not int:
        raise TypeError("amount_scale must be an integer")
    if not 0 <= value <= 12:
        raise ValueError("amount_scale must be between 0 and 12")
    return value


def _extraction_time(value: object) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("extraction_time must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("extraction_time must include a UTC offset")
    return value


__all__ = [
    "DgcSapTrialBalanceImportCommand",
    "DgcSapTrialBalanceImportResult",
    "DgcSapTrialBalanceImportService",
    "DgcSapTrialBalanceSource",
    "DgcTrialBalanceParameterValue",
    "dgc_trial_balance_scope_sha256",
]
