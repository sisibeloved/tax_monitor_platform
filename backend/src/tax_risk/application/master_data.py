"""Transactional import, approval, and point-in-time lookup for tax master data."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import re
import unicodedata
from uuid import UUID

from sqlalchemy import func, text

from tax_risk.adapters.ingest.base import fits_database_amount
from tax_risk.adapters.ingest.tax_master_xlsx import (
    DEFAULT_XLSX_RESOURCE_LIMITS,
    TaxMasterRow,
    TaxMasterRowError,
    TaxMasterWorkbookError,
    TaxMasterXlsxAdapter,
    XlsxResourceLimits,
)
from tax_risk.domain.money import Money
from tax_risk.persistence.ingest_models import (
    Company,
    CompanyLifecycle,
    IngestBatch,
    IngestBatchStatus,
    IngestError,
    IngestMode,
)
from tax_risk.persistence.master_models import TaxMasterVersion, VersionStatus
from tax_risk.persistence.repositories import UnitOfWork


UowFactory = Callable[[], UnitOfWork]
_CURRENCY = re.compile(r"[A-Z]{3}")


@dataclass(frozen=True, slots=True)
class MasterDataIssue:
    row_number: int
    error_code: str
    message: str
    field: str | None = None
    rejected_value: str | None = None


@dataclass(frozen=True, slots=True)
class TaxMasterImportResult:
    batch_id: UUID
    checksum: str
    source_filename: str
    uploaded_by: str
    currency: str
    amount_scale: int
    version_ids: tuple[UUID, ...]
    imported_at: datetime
    replayed: bool


@dataclass(frozen=True, slots=True)
class TaxMasterView:
    id: UUID
    source_batch_id: UUID
    company_code: str
    company_name: str
    valid_from: date
    valid_to: date | None
    version: str
    status: VersionStatus
    tax_rate: Decimal
    loss_carryforward: Decimal
    three_year_average_tax_burden: Decimal
    currency: str
    amount_scale: int
    source_filename: str | None
    source_checksum: str | None
    source_row_number: int
    uploaded_by: str
    imported_at: datetime
    published_at: datetime | None
    approved_by: str | None


class MasterDataError(Exception):
    error_code = "MASTER_DATA_ERROR"


class MasterDataValidationError(MasterDataError):
    error_code = "MASTER_DATA_VALIDATION_FAILED"

    def __init__(
        self,
        issues: tuple[MasterDataIssue, ...],
        *,
        batch_id: UUID | None = None,
    ) -> None:
        self.issues = issues
        self.batch_id = batch_id
        super().__init__(issues[0].message if issues else "tax master validation failed")


class MasterDataConflictError(MasterDataError):
    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        super().__init__(message)


class MasterDataNotFoundError(MasterDataError):
    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        super().__init__(message)


class InvalidImportOptionsError(MasterDataValidationError):
    error_code = "INVALID_IMPORT_OPTIONS"


class TaxMasterService:
    def __init__(
        self,
        uow_factory: UowFactory,
        *,
        xlsx_limits: XlsxResourceLimits = DEFAULT_XLSX_RESOURCE_LIMITS,
    ) -> None:
        self._uow_factory = uow_factory
        self._xlsx_limits = xlsx_limits

    def import_xlsx(
        self,
        *,
        filename: str,
        payload: bytes,
        uploaded_by: str,
        currency: str = "CNY",
        amount_scale: int = 2,
    ) -> TaxMasterImportResult:
        filename = _required_filename(filename)
        uploaded_by = _normalize_identity(uploaded_by, "uploaded_by")
        currency = _validate_currency(currency)
        amount_scale = _validate_amount_scale(amount_scale)
        checksum = sha256(payload).hexdigest()
        source_batch_key = _source_batch_key(
            uploaded_by=uploaded_by,
            checksum=checksum,
            filename=filename,
            currency=currency,
            amount_scale=amount_scale,
        )
        imported_at = datetime.now(timezone.utc)
        replayed = self._probe_replay(
            source_batch_key=source_batch_key,
            uploaded_by=uploaded_by,
            checksum=checksum,
            filename=filename,
            currency=currency,
            amount_scale=amount_scale,
        )
        if replayed is not None:
            return replayed

        adapter = TaxMasterXlsxAdapter(
            payload,
            amount_scale=amount_scale,
            limits=self._xlsx_limits,
        )
        workbook_error: TaxMasterWorkbookError | None = None
        try:
            rows = adapter.parse()
        except TaxMasterWorkbookError as error:
            workbook_error = error
            rows = error.valid_rows

        with self._uow_factory() as uow:
            _lock_source_batch_key(uow, source_batch_key)
            existing = uow.ingest.get_batch_by_source_key("TAX_MASTER_XLSX", source_batch_key)
            if existing is not None:
                return self._replayed_result(
                    uow,
                    existing,
                    uploaded_by=uploaded_by,
                    checksum=checksum,
                    filename=filename,
                    currency=currency,
                    amount_scale=amount_scale,
                )

            if workbook_error is not None:
                issues = [_adapter_issue(item) for item in workbook_error.errors]
                total = workbook_error.loss_control_total
                if total is not None and not fits_database_amount(total):
                    issues.append(_control_total_issue(total))
                    total = None
                issues = _sorted_issues(issues)
                batch = _new_ingest_batch(
                    source_batch_key=source_batch_key,
                    filename=filename,
                    checksum=checksum,
                    uploaded_by=uploaded_by,
                    currency=currency,
                    amount_scale=amount_scale,
                    imported_at=imported_at,
                    period=(
                        max(row.valid_from for row in rows)
                        if rows
                        else imported_at.date()
                    ),
                    status=IngestBatchStatus.FAILED,
                    record_count=workbook_error.record_count,
                    accepted_count=0,
                    rejected_count=workbook_error.record_count,
                    control_total=total if total is not None else Decimal("0"),
                )
                uow.ingest.add_batch(batch)
                uow.session.flush()
                _add_ingest_errors(uow, batch.id, issues)
                uow.commit()
                raise MasterDataValidationError(tuple(issues), batch_id=batch.id)

            companies = uow.ingest.lock_companies_shared(row.company_code for row in rows)
            issues = _company_issues(rows, companies)
            total = _loss_control_total(rows, currency, amount_scale)
            if not fits_database_amount(total):
                issues.append(_control_total_issue(total))
                persisted_total = Decimal("0")
            else:
                persisted_total = total
            issues = _sorted_issues(issues)

            batch = _new_ingest_batch(
                source_batch_key=source_batch_key,
                filename=filename,
                checksum=checksum,
                uploaded_by=uploaded_by,
                currency=currency,
                amount_scale=amount_scale,
                imported_at=imported_at,
                period=max(row.valid_from for row in rows),
                status=(IngestBatchStatus.FAILED if issues else IngestBatchStatus.SUCCEEDED),
                record_count=len(rows),
                accepted_count=(0 if issues else len(rows)),
                rejected_count=(len(rows) if issues else 0),
                control_total=persisted_total,
            )
            uow.ingest.add_batch(batch)
            uow.session.flush()
            if issues:
                _add_ingest_errors(uow, batch.id, issues)
                uow.commit()
                raise MasterDataValidationError(tuple(issues), batch_id=batch.id)

            versions = [
                _new_version(
                    row,
                    company=companies[row.company_code],
                    batch=batch,
                    filename=filename,
                    checksum=checksum,
                    uploaded_by=uploaded_by,
                    currency=currency,
                    amount_scale=amount_scale,
                )
                for row in rows
            ]
            for version in versions:
                uow.master.add_tax_master(version)
            uow.session.flush()
            uow.session.refresh(batch)
            result = TaxMasterImportResult(
                batch_id=batch.id,
                checksum=batch.checksum,
                source_filename=batch.payload_ref or filename,
                uploaded_by=uploaded_by,
                currency=batch.currency,
                amount_scale=batch.amount_scale,
                version_ids=tuple(version.id for version in versions),
                imported_at=batch.created_at,
                replayed=False,
            )
            uow.commit()
            return result

    def _probe_replay(
        self,
        *,
        source_batch_key: str,
        uploaded_by: str,
        checksum: str,
        filename: str,
        currency: str,
        amount_scale: int,
    ) -> TaxMasterImportResult | None:
        with self._uow_factory() as uow:
            existing = uow.ingest.get_batch_by_source_key(
                "TAX_MASTER_XLSX",
                source_batch_key,
            )
            if existing is None:
                return None
            return self._replayed_result(
                uow,
                existing,
                uploaded_by=uploaded_by,
                checksum=checksum,
                filename=filename,
                currency=currency,
                amount_scale=amount_scale,
            )

    def approve(self, version_id: UUID, *, reviewed_by: str) -> TaxMasterView:
        reviewed_by = _normalize_identity(reviewed_by, "reviewed_by")
        with self._uow_factory() as uow:
            version = uow.master.get_tax_master(version_id, for_update=True)
            if version is None:
                raise MasterDataNotFoundError(
                    "TAX_MASTER_VERSION_NOT_FOUND",
                    f"tax master version {version_id} was not found",
                )
            company = uow.ingest.get_company(version.company_id)
            if company is None:
                raise MasterDataConflictError(
                    "TAX_MASTER_COMPANY_MISSING",
                    "tax master version references a missing company",
                )
            if version.status != VersionStatus.DRAFT:
                raise MasterDataConflictError(
                    "TAX_MASTER_STATE_CONFLICT",
                    f"tax master version cannot be approved from {version.status.value}",
                )
            if reviewed_by == version.uploaded_by:
                raise MasterDataConflictError(
                    "MAKER_REVIEWER_CONFLICT",
                    "reviewer must be different from the uploader",
                )

            locked = uow.ingest.lock_companies_exclusive({company.company_code})
            company = locked[company.company_code]
            if company is None:
                raise MasterDataConflictError(
                    "INACTIVE_COMPANY",
                    "only active controlled companies can publish tax master data",
                )
            uow.session.refresh(company)
            if company.lifecycle != CompanyLifecycle.ACTIVE:
                raise MasterDataConflictError(
                    "INACTIVE_COMPANY",
                    "only active controlled companies can publish tax master data",
                )
            imported_company_name = version.data.get("company_name")
            if (
                not isinstance(imported_company_name, str)
                or company.company_name != imported_company_name
            ):
                raise MasterDataConflictError(
                    "COMPANY_NAME_MISMATCH",
                    "current controlled company_name differs from the imported tax master",
                )
            overlaps = uow.master.overlapping_published_tax_masters(version)
            if overlaps:
                raise MasterDataConflictError(
                    "PUBLISHED_PERIOD_OVERLAP",
                    "published tax master effective periods must not overlap",
                )

            version.status = VersionStatus.PUBLISHED
            version.published_at = func.now()
            version.approved_by = reviewed_by
            uow.session.flush()
            uow.session.refresh(version)
            view = _view(version, company)
            uow.commit()
            return view

    def lookup(self, company_code: str, *, effective_on: date) -> TaxMasterView:
        company_code = _required_text(company_code, "company_code", 64)
        if type(effective_on) is not date:
            raise TypeError("effective_on must be a date")
        with self._uow_factory() as uow:
            company = uow.ingest.get_company_by_code(company_code)
            if company is None:
                raise MasterDataNotFoundError(
                    "TAX_MASTER_NOT_FOUND",
                    f"no published tax master exists for {company_code}",
                )
            matches = uow.master.published_tax_masters(company.id, effective_on)
            if not matches:
                raise MasterDataNotFoundError(
                    "TAX_MASTER_NOT_FOUND",
                    f"no published tax master exists for {company_code} on {effective_on}",
                )
            if len(matches) != 1:
                raise MasterDataConflictError(
                    "MULTIPLE_PUBLISHED_TAX_MASTERS",
                    "multiple published tax masters match the requested company and period",
                )
            return _view(matches[0], company)

    @staticmethod
    def _replayed_result(
        uow: UnitOfWork,
        batch: IngestBatch,
        *,
        uploaded_by: str,
        checksum: str,
        filename: str,
        currency: str,
        amount_scale: int,
    ) -> TaxMasterImportResult:
        if (
            batch.dataset_code != "tax_master"
            or batch.checksum != checksum
            or batch.payload_ref != filename
            or batch.currency != currency
            or batch.amount_scale != amount_scale
            or batch.source_primary_key_definition.get("uploaded_by") != uploaded_by
            or batch.source_primary_key_definition.get("source_filename") != filename
        ):
            raise MasterDataConflictError(
                "TAX_MASTER_IDEMPOTENCY_CONFLICT",
                "the idempotency key already exists with different import metadata",
            )
        if batch.status == IngestBatchStatus.FAILED:
            persisted_errors = uow.ingest.list_errors(batch.id)
            if not persisted_errors:
                raise MasterDataConflictError(
                    "TAX_MASTER_LINEAGE_CONFLICT",
                    "failed tax master source batch has no persisted validation errors",
                )
            issues = _sorted_issues(
                _ingest_error_issue(error) for error in persisted_errors
            )
            raise MasterDataValidationError(tuple(issues), batch_id=batch.id)
        if batch.status != IngestBatchStatus.SUCCEEDED:
            raise MasterDataConflictError(
                "TAX_MASTER_STATE_CONFLICT",
                f"tax master import cannot replay from {batch.status.value}",
            )
        versions = uow.master.tax_masters_for_source_batch(batch.id)
        if len(versions) != batch.accepted_count:
            raise MasterDataConflictError(
                "TAX_MASTER_LINEAGE_CONFLICT",
                "tax master source batch does not reconcile to its draft versions",
            )
        return TaxMasterImportResult(
            batch_id=batch.id,
            checksum=batch.checksum,
            source_filename=batch.payload_ref or "upload.xlsx",
            uploaded_by=uploaded_by,
            currency=batch.currency,
            amount_scale=batch.amount_scale,
            version_ids=tuple(version.id for version in versions),
            imported_at=batch.created_at,
            replayed=True,
        )


def _source_batch_key(
    *,
    uploaded_by: str,
    checksum: str,
    filename: str,
    currency: str,
    amount_scale: int,
) -> str:
    canonical = "\0".join(
        (uploaded_by, filename, currency, str(amount_scale), checksum)
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _new_ingest_batch(
    *,
    source_batch_key: str,
    filename: str,
    checksum: str,
    uploaded_by: str,
    currency: str,
    amount_scale: int,
    imported_at: datetime,
    period: date,
    status: IngestBatchStatus,
    record_count: int,
    accepted_count: int,
    rejected_count: int,
    control_total: Decimal,
) -> IngestBatch:
    return IngestBatch(
        source="TAX_MASTER_XLSX",
        source_batch_key=source_batch_key,
        dataset_code="tax_master",
        status=status,
        extraction_time=imported_at,
        period=period,
        mode=IngestMode.FULL,
        schema_version="tax-master-xlsx-v1",
        payload_ref=filename,
        source_primary_key_definition={
            "fields": ["company_code", "valid_from", "source_row_number"],
            "uploaded_by": uploaded_by,
            "source_filename": filename,
            "currency": currency,
            "amount_scale": amount_scale,
        },
        currency=currency,
        amount_scale=amount_scale,
        record_count=record_count,
        accepted_count=accepted_count,
        rejected_count=rejected_count,
        control_total=control_total,
        checksum=checksum,
    )


def _add_ingest_errors(
    uow: UnitOfWork,
    batch_id: UUID,
    issues: Iterable[MasterDataIssue],
) -> None:
    for issue in issues:
        uow.ingest.add_error(
            IngestError(
                batch_id=batch_id,
                row_number=issue.row_number,
                error_code=issue.error_code,
                message=issue.message,
                details={
                    "field": issue.field,
                    "rejected_value": issue.rejected_value,
                },
                retryable=False,
            )
        )


def _sorted_issues(issues: Iterable[MasterDataIssue]) -> list[MasterDataIssue]:
    return sorted(
        issues,
        key=lambda issue: (
            issue.row_number,
            issue.field or "",
            issue.error_code,
        ),
    )


def _control_total_issue(total: Decimal) -> MasterDataIssue:
    return MasterDataIssue(
        1,
        "CONTROL_TOTAL_OUT_OF_RANGE",
        "loss carryforward control total exceeds NUMERIC(38, 12)",
        "loss_carryforward",
        str(total),
    )


def _new_version(
    row: TaxMasterRow,
    *,
    company: Company | None,
    batch: IngestBatch,
    filename: str,
    checksum: str,
    uploaded_by: str,
    currency: str,
    amount_scale: int,
) -> TaxMasterVersion:
    assert company is not None
    return TaxMasterVersion(
        company_id=company.id,
        source_batch_id=batch.id,
        valid_from=row.valid_from,
        valid_to=row.valid_to,
        version=f"{batch.id.hex[:20]}-r{row.row_number}",
        status=VersionStatus.DRAFT,
        tax_rate=row.tax_rate.value,
        loss_carryforward=row.loss_carryforward,
        average_tax_burden_rate_3y=row.three_year_average_tax_burden.value,
        currency=currency,
        amount_scale=amount_scale,
        source_file_name=filename,
        source_checksum=checksum,
        source_row_number=row.row_number,
        uploaded_by=uploaded_by,
        data={
            "company_code": row.company_code,
            "company_name": row.company_name,
            "valid_from": row.valid_from.isoformat(),
            "valid_to": row.valid_to.isoformat() if row.valid_to else None,
            "tax_rate": str(row.tax_rate.value),
            "loss_carryforward": str(row.loss_carryforward),
            "three_year_average_tax_burden": str(
                row.three_year_average_tax_burden.value
            ),
        },
    )


def _company_issues(
    rows: tuple[TaxMasterRow, ...],
    companies: dict[str, Company | None],
) -> list[MasterDataIssue]:
    issues: list[MasterDataIssue] = []
    for row in rows:
        company = companies[row.company_code]
        if company is None:
            issues.append(
                MasterDataIssue(
                    row.row_number,
                    "UNKNOWN_COMPANY",
                    "company_code does not exist in the controlled company master",
                    "company_code",
                    row.company_code,
                )
            )
        elif company.lifecycle != CompanyLifecycle.ACTIVE:
            issues.append(
                MasterDataIssue(
                    row.row_number,
                    "INACTIVE_COMPANY",
                    "company_code is inactive in the controlled company master",
                    "company_code",
                    row.company_code,
                )
            )
        elif company.company_name != row.company_name:
            issues.append(
                MasterDataIssue(
                    row.row_number,
                    "COMPANY_NAME_MISMATCH",
                    "company_name does not match the controlled company master",
                    "company_name",
                    row.company_name,
                )
            )
    return issues


def _loss_control_total(
    rows: tuple[TaxMasterRow, ...],
    currency: str,
    amount_scale: int,
) -> Decimal:
    total = Money.unrounded("0", currency=currency, scale=amount_scale)
    for row in rows:
        total += Money.unrounded(
            row.loss_carryforward,
            currency=currency,
            scale=amount_scale,
        )
    return total.amount


def _lock_source_batch_key(uow: UnitOfWork, source_batch_key: str) -> None:
    uow.session.execute(
        text(
            "SELECT pg_advisory_xact_lock(:lock_namespace, hashtext(:source_batch_key))"
        ),
        {"lock_namespace": 20260713, "source_batch_key": source_batch_key},
    )


def _view(version: TaxMasterVersion, company: Company) -> TaxMasterView:
    return TaxMasterView(
        id=version.id,
        source_batch_id=version.source_batch_id,
        company_code=company.company_code,
        company_name=company.company_name,
        valid_from=version.valid_from,
        valid_to=version.valid_to,
        version=version.version,
        status=version.status,
        tax_rate=version.tax_rate,
        loss_carryforward=version.loss_carryforward,
        three_year_average_tax_burden=version.average_tax_burden_rate_3y,
        currency=version.currency,
        amount_scale=version.amount_scale,
        source_filename=version.source_file_name,
        source_checksum=version.source_checksum,
        source_row_number=version.source_row_number,
        uploaded_by=version.uploaded_by,
        imported_at=version.created_at,
        published_at=version.published_at,
        approved_by=version.approved_by,
    )


def _adapter_issue(error: TaxMasterRowError) -> MasterDataIssue:
    return MasterDataIssue(
        row_number=error.row_number,
        error_code=error.error_code,
        message=error.message,
        field=error.field,
        rejected_value=error.rejected_value,
    )


def _ingest_error_issue(error: IngestError) -> MasterDataIssue:
    field = error.details.get("field")
    rejected_value = error.details.get("rejected_value")
    return MasterDataIssue(
        row_number=error.row_number,
        error_code=error.error_code,
        message=error.message,
        field=field if isinstance(field, str) else None,
        rejected_value=(
            rejected_value if isinstance(rejected_value, str) else None
        ),
    )


def _required_text(value: object, field: str, maximum_length: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum_length:
        raise InvalidImportOptionsError(
            (
                MasterDataIssue(
                    1,
                    "INVALID_IMPORT_OPTIONS",
                    f"{field} must be non-blank and at most {maximum_length} characters",
                    field,
                    None if value is None else str(value),
                ),
            )
        )
    return value.strip()


def _required_filename(value: object) -> str:
    normalized = (
        unicodedata.normalize("NFC", value).strip()
        if isinstance(value, str)
        else ""
    )
    if (
        not isinstance(value, str)
        or _has_control_or_format(value)
        or _has_control_or_format(normalized)
        or not normalized
        or len(normalized) > 1024
    ):
        raise InvalidImportOptionsError(
            (
                MasterDataIssue(
                    1,
                    "INVALID_IMPORT_OPTIONS",
                    "filename must be non-blank and at most 1024 characters",
                    "filename",
                    None if value is None else str(value),
                ),
            )
        )
    return normalized


def _normalize_identity(value: object, field: str) -> str:
    normalized = (
        unicodedata.normalize("NFKC", value).strip().casefold()
        if isinstance(value, str)
        else ""
    )
    if (
        not isinstance(value, str)
        or _has_control_or_format(value)
        or _has_control_or_format(normalized)
        or not normalized
        or len(normalized) > 256
    ):
        raise InvalidImportOptionsError(
            (
                MasterDataIssue(
                    1,
                    "INVALID_IMPORT_OPTIONS",
                    f"{field} contains invalid identity characters or length",
                    field,
                    None if value is None else str(value),
                ),
            )
        )
    return normalized


def _has_control_or_format(value: str) -> bool:
    return any(unicodedata.category(character) in {"Cc", "Cf"} for character in value)


def _validate_currency(currency: object) -> str:
    if not isinstance(currency, str):
        normalized = ""
    else:
        normalized = currency.strip().upper()
    if _CURRENCY.fullmatch(normalized) is None:
        raise InvalidImportOptionsError(
            (
                MasterDataIssue(
                    1,
                    "INVALID_IMPORT_OPTIONS",
                    "currency must be three uppercase letters",
                    "currency",
                    str(currency),
                ),
            )
        )
    return normalized


def _validate_amount_scale(amount_scale: object) -> int:
    if type(amount_scale) is not int or not 0 <= amount_scale <= 12:
        raise InvalidImportOptionsError(
            (
                MasterDataIssue(
                    1,
                    "INVALID_IMPORT_OPTIONS",
                    "amount_scale must be an integer between 0 and 12",
                    "amount_scale",
                    str(amount_scale),
                ),
            )
        )
    return amount_scale


__all__ = [
    "InvalidImportOptionsError",
    "MasterDataConflictError",
    "MasterDataError",
    "MasterDataIssue",
    "MasterDataNotFoundError",
    "MasterDataValidationError",
    "TaxMasterImportResult",
    "TaxMasterService",
    "TaxMasterView",
]
