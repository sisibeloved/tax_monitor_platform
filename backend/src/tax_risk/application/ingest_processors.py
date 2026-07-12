from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from tax_risk.adapters.ingest.base import (
    AdapterRow,
    CanonicalFinancialRow,
    CompanyMasterRow,
    RowError,
    fits_database_amount,
)
from tax_risk.domain.money import Money
from tax_risk.persistence.ingest_models import (
    Company,
    CompanyLifecycle,
    IngestBatch,
    SourceRecord,
)
from tax_risk.persistence.repositories import UnitOfWork


@dataclass(frozen=True, slots=True)
class ProcessingResult:
    record_count: int
    accepted_count: int
    rejected_count: int
    control_total: Decimal
    errors: tuple[RowError, ...]
    source_records: tuple[SourceRecord, ...] = ()


class CompanyMasterProcessor:
    """Apply event-time ordered company master changes under per-company locks."""

    def process(
        self,
        rows: Iterable[AdapterRow],
        *,
        uow: UnitOfWork,
        batch: IngestBatch,
        checksum: str,
    ) -> ProcessingResult:
        del checksum
        materialized = list(rows)
        record_count = len(materialized)
        accepted_count = 0
        errors: list[RowError] = []
        seen_source_keys: set[str] = set()
        seen_company_codes: set[str] = set()
        candidates: list[tuple[int, CompanyMasterRow]] = []

        for adapted in materialized:
            if adapted.error is not None:
                errors.append(adapted.error)
                continue
            assert adapted.value is not None
            if not isinstance(adapted.value, CompanyMasterRow):
                errors.append(_unexpected_row_type(adapted.row_number, "company master"))
                continue
            row = adapted.value
            duplicate = _duplicate_error(
                row.source_record_key,
                row.company_code,
                adapted.row_number,
                seen_source_keys,
                seen_company_codes,
            )
            if duplicate is not None:
                errors.append(duplicate)
                continue
            candidates.append((adapted.row_number, row))

        companies = uow.ingest.lock_companies_exclusive({row.company_code for _, row in candidates})
        for row_number, row in candidates:
            event_error = self._apply_event(
                uow,
                batch,
                row,
                row_number,
                companies[row.company_code],
            )
            if event_error is not None:
                errors.append(event_error)
                continue
            accepted_count += 1

        return ProcessingResult(
            record_count=record_count,
            accepted_count=accepted_count,
            rejected_count=len(errors),
            control_total=Decimal("0"),
            errors=tuple(errors),
        )

    @staticmethod
    def _apply_event(
        uow: UnitOfWork,
        batch: IngestBatch,
        row: CompanyMasterRow,
        row_number: int,
        company: Company | None,
    ) -> RowError | None:
        desired = CompanyLifecycle(row.lifecycle)
        if company is None:
            uow.ingest.add_company(
                Company(
                    company_code=row.company_code,
                    company_name=row.company_name,
                    lifecycle=desired,
                    master_data_updated_at=row.extracted_at,
                    lifecycle_changed_at=row.extracted_at,
                    deactivated_at=(
                        row.extracted_at if desired == CompanyLifecycle.INACTIVE else None
                    ),
                    lifecycle_reason="company_master_import",
                    lifecycle_changed_by=batch.source,
                )
            )
            return None

        if row.extracted_at < company.master_data_updated_at:
            return RowError(
                row_number,
                "STALE_COMPANY_MASTER_EVENT",
                "company master event is older than the latest applied event",
                "extracted_at",
                row.extracted_at.isoformat(),
            )
        if row.extracted_at == company.master_data_updated_at:
            if company.company_name == row.company_name and company.lifecycle == desired:
                return None
            return RowError(
                row_number,
                "COMPANY_MASTER_EVENT_CONFLICT",
                "company master event time already exists with different state",
                "extracted_at",
                row.extracted_at.isoformat(),
            )

        company.company_name = row.company_name
        company.master_data_updated_at = row.extracted_at
        if company.lifecycle != desired:
            company.lifecycle = desired
            company.lifecycle_changed_at = row.extracted_at
            company.deactivated_at = (
                row.extracted_at if desired == CompanyLifecycle.INACTIVE else None
            )
            company.lifecycle_reason = "company_master_import"
            company.lifecycle_changed_by = batch.source
        return None


class FinancialProcessor:
    """Validate canonical financial rows and reconcile only the final control total."""

    def process(
        self,
        rows: Iterable[AdapterRow],
        *,
        uow: UnitOfWork,
        batch: IngestBatch,
        checksum: str,
    ) -> ProcessingResult:
        materialized = list(rows)
        record_count = len(materialized)
        valid_count = 0
        errors: list[RowError] = []
        pending_records: list[SourceRecord] = []
        seen_source_keys: set[str] = set()
        candidates: list[tuple[int, CanonicalFinancialRow]] = []
        control_total = Money.unrounded(
            "0",
            currency=batch.currency,
            scale=batch.amount_scale,
        )

        for adapted in materialized:
            if adapted.error is not None:
                errors.append(adapted.error)
                continue
            assert adapted.value is not None
            if not isinstance(adapted.value, CanonicalFinancialRow):
                errors.append(_unexpected_row_type(adapted.row_number, "financial"))
                continue
            row = adapted.value
            if row.source_record_key in seen_source_keys:
                errors.append(
                    RowError(
                        adapted.row_number,
                        "DUPLICATE_SOURCE_RECORD_KEY",
                        "source_record_key is duplicated within the file",
                        "source_record_key",
                        row.source_record_key,
                    )
                )
                continue
            seen_source_keys.add(row.source_record_key)
            row_error = self._metadata_error(batch, row, adapted.row_number)
            if row_error is not None:
                errors.append(row_error)
                continue
            candidates.append((adapted.row_number, row))

        company_cache = uow.ingest.lock_companies_shared(
            {row.company_code for _, row in candidates}
        )
        for row_number, row in candidates:
            company = company_cache[row.company_code]
            row_error = self._company_error(row, row_number, company)
            if row_error is not None:
                errors.append(row_error)
                continue
            assert company is not None
            control_total = control_total + Money.unrounded(
                row.amount,
                currency=row.currency,
                scale=row.amount_scale,
            )
            pending_records.append(
                SourceRecord(
                    batch_id=batch.id,
                    source_record_key=row.source_record_key,
                    company_id=company.id,
                    dataset_code=batch.dataset_code,
                    period=row.period,
                    currency=row.currency,
                    amount_scale=row.amount_scale,
                    amount=row.amount,
                    payload=_financial_payload(row),
                    lineage={
                        "source": batch.source,
                        "source_batch_key": batch.source_batch_key,
                        "checksum": checksum,
                        "row_number": row_number,
                    },
                    extracted_at=row.extracted_at,
                )
            )
            valid_count += 1

        if valid_count and not fits_database_amount(control_total.amount):
            errors.append(
                RowError(
                    1,
                    "CONTROL_TOTAL_OUT_OF_RANGE",
                    "final accepted control total exceeds NUMERIC(38, 12)",
                )
            )
            return ProcessingResult(
                record_count=record_count,
                accepted_count=0,
                rejected_count=record_count,
                control_total=Decimal("0"),
                errors=tuple(errors),
            )

        return ProcessingResult(
            record_count=record_count,
            accepted_count=valid_count,
            rejected_count=len(errors),
            control_total=control_total.amount,
            errors=tuple(errors),
            source_records=tuple(pending_records),
        )

    @staticmethod
    def _metadata_error(
        batch: IngestBatch,
        row: CanonicalFinancialRow,
        row_number: int,
    ) -> RowError | None:
        if row.currency != batch.currency:
            return RowError(
                row_number,
                "BATCH_METADATA_MISMATCH",
                "row currency does not match batch currency",
                "currency",
                row.currency,
            )
        if row.amount_scale != batch.amount_scale:
            return RowError(
                row_number,
                "BATCH_METADATA_MISMATCH",
                "row amount_scale does not match batch amount_scale",
                "amount_scale",
                str(row.amount_scale),
            )
        if row.period != batch.period:
            return RowError(
                row_number,
                "BATCH_METADATA_MISMATCH",
                "row period does not match batch period",
                "period",
                row.period.isoformat(),
            )
        if not fits_database_amount(row.amount):
            return RowError(
                row_number,
                "DECIMAL_OUT_OF_RANGE",
                "amount exceeds NUMERIC(38, 12)",
                "amount",
                str(row.amount),
            )
        return None

    @staticmethod
    def _company_error(
        row: CanonicalFinancialRow,
        row_number: int,
        company: Company | None,
    ) -> RowError | None:
        if company is None:
            return RowError(
                row_number,
                "UNKNOWN_COMPANY",
                "company_code does not exist in the controlled company master",
                "company_code",
                row.company_code,
            )
        if company.lifecycle != CompanyLifecycle.ACTIVE:
            return RowError(
                row_number,
                "INACTIVE_COMPANY",
                "company_code is inactive in the controlled company master",
                "company_code",
                row.company_code,
            )
        return None


def _duplicate_error(
    source_record_key: str,
    company_code: str,
    row_number: int,
    seen_source_keys: set[str],
    seen_company_codes: set[str],
) -> RowError | None:
    if source_record_key in seen_source_keys:
        return RowError(
            row_number,
            "DUPLICATE_SOURCE_RECORD_KEY",
            "source_record_key is duplicated within the file",
            "source_record_key",
            source_record_key,
        )
    seen_source_keys.add(source_record_key)
    if company_code in seen_company_codes:
        return RowError(
            row_number,
            "DUPLICATE_COMPANY_CODE",
            "company_code is duplicated within the company master file",
            "company_code",
            company_code,
        )
    seen_company_codes.add(company_code)
    return None


def _unexpected_row_type(row_number: int, expected: str) -> RowError:
    return RowError(
        row_number,
        "DATASET_ROW_TYPE_MISMATCH",
        f"adapter row is not a canonical {expected} row",
    )


def _financial_payload(row: CanonicalFinancialRow) -> dict[str, Any]:
    return {
        "source_record_key": row.source_record_key,
        "company_code": row.company_code,
        "fiscal_year": row.fiscal_year,
        "period": row.period.isoformat(),
        "currency": row.currency,
        "amount_scale": row.amount_scale,
        "metric_code": row.metric_code,
        "amount": str(row.amount),
        "extracted_at": row.extracted_at.isoformat(),
    }


__all__ = ["CompanyMasterProcessor", "FinancialProcessor", "ProcessingResult"]
