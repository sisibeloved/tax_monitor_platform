from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any, cast

from tax_risk.adapters.ingest.base import (
    AdapterRow,
    CanonicalFinancialRow,
    CompanyMasterRow,
    RowError,
    fits_database_amount,
)
from tax_risk.domain.money import Money
from tax_risk.domain.business_entertainment.source_models import (
    BusinessEntertainmentSourceRecord,
    HesiBusinessEntertainmentRecord,
    OaBusinessEntertainmentRecord,
    OaMaterialRequisitionRecord,
    OaSelfProcurementRecord,
)
from tax_risk.domain.semantic.sap_voucher import SapExpenseVoucherRecord
from tax_risk.persistence.business_entertainment_models import (
    BusinessEntertainmentSourceObservation,
)
from tax_risk.persistence.ingest_models import (
    Company,
    CompanyLifecycle,
    IngestBatch,
    SourceRecord,
)
from tax_risk.persistence.semantic_models import SapExpenseVoucherObservation
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
                        _financial_context(row),
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
                _financial_context(row),
            )
        if row.amount_scale != batch.amount_scale:
            return RowError(
                row_number,
                "BATCH_METADATA_MISMATCH",
                "row amount_scale does not match batch amount_scale",
                "amount_scale",
                str(row.amount_scale),
                _financial_context(row),
            )
        if row.period != batch.period:
            return RowError(
                row_number,
                "BATCH_METADATA_MISMATCH",
                "row period does not match batch period",
                "period",
                row.period.isoformat(),
                _financial_context(row),
            )
        if not fits_database_amount(row.amount):
            return RowError(
                row_number,
                "DECIMAL_OUT_OF_RANGE",
                "amount exceeds NUMERIC(38, 12)",
                "amount",
                str(row.amount),
                _financial_context(row),
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
                _financial_context(row),
            )
        if company.lifecycle != CompanyLifecycle.ACTIVE:
            return RowError(
                row_number,
                "INACTIVE_COMPANY",
                "company_code is inactive in the controlled company master",
                "company_code",
                row.company_code,
                _financial_context(row),
            )
        return None


class BusinessEntertainmentSourceProcessor:
    """Persist governed evidence rows and their immutable normalized indexes."""

    def process(
        self,
        rows: Iterable[AdapterRow],
        *,
        uow: UnitOfWork,
        batch: IngestBatch,
        checksum: str,
    ) -> ProcessingResult:
        materialized = list(rows)
        errors: list[RowError] = []
        seen: set[str] = set()
        candidates: list[
            tuple[int, SapExpenseVoucherRecord | BusinessEntertainmentSourceRecord]
        ] = []
        for adapted in materialized:
            if adapted.error is not None:
                errors.append(adapted.error)
                continue
            value = adapted.value
            if not isinstance(
                value,
                (
                    SapExpenseVoucherRecord,
                    HesiBusinessEntertainmentRecord,
                    OaBusinessEntertainmentRecord,
                    OaSelfProcurementRecord,
                    OaMaterialRequisitionRecord,
                ),
            ):
                errors.append(_unexpected_row_type(adapted.row_number, "entertainment source"))
                continue
            record = cast(
                SapExpenseVoucherRecord | BusinessEntertainmentSourceRecord,
                value,
            )
            if record.source_record_key in seen:
                errors.append(
                    RowError(
                        adapted.row_number,
                        "DUPLICATE_SOURCE_RECORD_KEY",
                        "source_record_key is duplicated within the file",
                        "source_record_key",
                        record.source_record_key,
                    )
                )
                continue
            seen.add(record.source_record_key)
            metadata_error = _business_source_metadata_error(batch, record, adapted.row_number)
            if metadata_error is not None:
                errors.append(metadata_error)
                continue
            candidates.append((adapted.row_number, record))

        companies = uow.ingest.lock_companies_shared(
            record.company_code for _, record in candidates
        )
        accepted: list[
            tuple[int, SapExpenseVoucherRecord | BusinessEntertainmentSourceRecord, Company]
        ] = []
        total = Money.unrounded("0", currency=batch.currency, scale=batch.amount_scale)
        for row_number, candidate in candidates:
            company = companies[candidate.company_code]
            company_error = _business_source_company_error(candidate, row_number, company)
            if company_error is not None:
                errors.append(company_error)
                continue
            assert company is not None
            amount = getattr(candidate, "amount", None)
            if amount is not None:
                total += Money.unrounded(
                    amount,
                    currency=cast(str, getattr(candidate, "currency")),
                    scale=batch.amount_scale,
                )
            accepted.append((row_number, candidate, company))

        if accepted and not fits_database_amount(total.amount):
            errors.append(
                RowError(
                    1,
                    "CONTROL_TOTAL_OUT_OF_RANGE",
                    "final accepted control total exceeds NUMERIC(38, 12)",
                )
            )
            return ProcessingResult(
                record_count=len(materialized),
                accepted_count=0,
                rejected_count=len(materialized),
                control_total=Decimal("0"),
                errors=tuple(errors),
            )

        for row_number, accepted_record, company in accepted:
            amount = getattr(accepted_record, "amount", None)
            source_record = SourceRecord(
                batch_id=batch.id,
                source_record_key=accepted_record.source_record_key,
                company_id=company.id,
                dataset_code=batch.dataset_code,
                period=batch.period,
                currency=batch.currency,
                amount_scale=batch.amount_scale,
                amount=amount,
                payload=accepted_record.model_dump(mode="json"),
                lineage={
                    "source": batch.source,
                    "source_batch_key": batch.source_batch_key,
                    "checksum": checksum,
                    "row_number": row_number,
                },
                extracted_at=batch.extraction_time,
            )
            uow.ingest.add_source_record(source_record)
            uow.session.flush()
            if isinstance(accepted_record, SapExpenseVoucherRecord):
                uow.semantic.add_sap_observation(
                    _sap_observation(batch, source_record, accepted_record)
                )
            else:
                uow.business_entertainment_scope.add_source_observation(
                    _business_observation(batch, source_record, accepted_record)
                )

        return ProcessingResult(
            record_count=len(materialized),
            accepted_count=len(accepted),
            rejected_count=len(errors),
            control_total=total.amount,
            errors=tuple(errors),
        )

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


def _business_source_metadata_error(
    batch: IngestBatch,
    record: SapExpenseVoucherRecord | BusinessEntertainmentSourceRecord,
    row_number: int,
) -> RowError | None:
    document_date = _business_document_date(record)
    fiscal_year = getattr(record, "fiscal_year", document_date.year)
    period = getattr(record, "period", document_date.month)
    if fiscal_year != batch.period.year or period != batch.period.month:
        return RowError(
            row_number,
            "BATCH_METADATA_MISMATCH",
            "row fiscal period does not match batch period",
            "period",
            f"{fiscal_year}-{period:02d}",
            (("company_code", record.company_code),),
        )
    amount = getattr(record, "amount", None)
    currency = getattr(record, "currency", None)
    if amount is None:
        return None
    if currency != batch.currency:
        return RowError(
            row_number,
            "BATCH_METADATA_MISMATCH",
            "row currency does not match batch currency",
            "currency",
            str(currency),
            (("company_code", record.company_code),),
        )
    exponent = cast(int, amount.as_tuple().exponent)
    if max(-exponent, 0) > batch.amount_scale:
        return RowError(
            row_number,
            "AMOUNT_SCALE_MISMATCH",
            "amount has more fractional digits than batch amount_scale",
            "amount",
            str(amount),
            (("company_code", record.company_code),),
        )
    if not fits_database_amount(amount):
        return RowError(
            row_number,
            "DECIMAL_OUT_OF_RANGE",
            "amount exceeds NUMERIC(38, 12)",
            "amount",
            str(amount),
            (("company_code", record.company_code),),
        )
    return None


def _business_source_company_error(
    record: SapExpenseVoucherRecord | BusinessEntertainmentSourceRecord,
    row_number: int,
    company: Company | None,
) -> RowError | None:
    if company is None:
        return RowError(
            row_number,
            "UNKNOWN_COMPANY",
            "company_code does not exist in the controlled company master",
            "company_code",
            record.company_code,
            (("company_code", record.company_code),),
        )
    if company.lifecycle != CompanyLifecycle.ACTIVE:
        return RowError(
            row_number,
            "INACTIVE_COMPANY",
            "company_code is inactive in the controlled company master",
            "company_code",
            record.company_code,
            (("company_code", record.company_code),),
        )
    return None


def _business_document_date(
    record: SapExpenseVoucherRecord | BusinessEntertainmentSourceRecord,
) -> date:
    if isinstance(record, SapExpenseVoucherRecord):
        return record.posting_date
    if isinstance(record, HesiBusinessEntertainmentRecord):
        return record.expense_date
    if isinstance(record, OaBusinessEntertainmentRecord):
        return record.application_date
    if isinstance(record, OaSelfProcurementRecord):
        return record.purchase_date
    return record.requisition_date


def _sap_observation(
    batch: IngestBatch,
    source_record: SourceRecord,
    record: SapExpenseVoucherRecord,
) -> SapExpenseVoucherObservation:
    return SapExpenseVoucherObservation(
        source_record_id=source_record.id,
        ingest_batch_id=batch.id,
        source_record_key=record.source_record_key,
        company_code=record.company_code,
        fiscal_year=record.fiscal_year,
        period=record.period,
        posting_date=record.posting_date,
        document_number=record.document_number,
        line_item=record.line_item,
        current_account_code=record.current_account_code,
        current_account_name=record.current_account_name,
        amount=record.amount,
        currency=record.currency,
        summary=record.summary,
        assignment=record.assignment,
        reference=record.reference,
        reversal_reference=record.reversal_reference,
        account_family=record.account_family.value,
    )


def _business_observation(
    batch: IngestBatch,
    source_record: SourceRecord,
    record: BusinessEntertainmentSourceRecord,
) -> BusinessEntertainmentSourceObservation:
    if isinstance(record, HesiBusinessEntertainmentRecord):
        document_id = record.expense_claim_id
        line_id = record.line_id
        parent_oa_id = record.related_oa_id
        parent_hesi_id = None
        fiscal_year = record.fiscal_year
        period = record.period
    elif isinstance(record, OaBusinessEntertainmentRecord):
        document_id = record.application_id
        line_id = record.line_id
        parent_oa_id = None
        parent_hesi_id = None
        fiscal_year = record.application_date.year
        period = record.application_date.month
    elif isinstance(record, OaSelfProcurementRecord):
        document_id = record.application_id
        line_id = record.line_id
        parent_oa_id = record.parent_oa_id
        parent_hesi_id = record.parent_hesi_id
        fiscal_year = record.purchase_date.year
        period = record.purchase_date.month
    else:
        document_id = record.requisition_id
        line_id = record.line_id
        parent_oa_id = record.parent_oa_id
        parent_hesi_id = record.parent_hesi_id
        fiscal_year = record.requisition_date.year
        period = record.requisition_date.month
    return BusinessEntertainmentSourceObservation(
        source_record_id=source_record.id,
        ingest_batch_id=batch.id,
        dataset_code=batch.dataset_code,
        source_record_key=record.source_record_key,
        company_code=record.company_code,
        fiscal_year=fiscal_year,
        period=period,
        document_date=_business_document_date(record),
        document_id=document_id,
        line_id=line_id,
        amount=getattr(record, "amount", None),
        currency=getattr(record, "currency", None),
        parent_oa_id=parent_oa_id,
        parent_hesi_id=parent_hesi_id,
    )


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


def _financial_context(row: CanonicalFinancialRow) -> tuple[tuple[str, str], ...]:
    return (
        ("company_code", row.company_code),
        ("metric_code", row.metric_code),
    )


__all__ = [
    "BusinessEntertainmentSourceProcessor",
    "CompanyMasterProcessor",
    "FinancialProcessor",
    "ProcessingResult",
]
