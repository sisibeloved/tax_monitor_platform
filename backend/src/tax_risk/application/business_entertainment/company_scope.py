"""Governed import, approval, publication, and resolution of company scope."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import PurePath
from uuid import UUID

from tax_risk.adapters.ingest.business_entertainment_company_list_xlsx import (
    BusinessEntertainmentScopeRow,
    BusinessEntertainmentScopeRowError,
    BusinessEntertainmentScopeWorkbookError,
    BusinessEntertainmentScopeXlsxAdapter,
)
from tax_risk.domain.business_entertainment.company_scope import (
    BusinessEntertainmentScopeResolution,
    BusinessEntertainmentScopeVersion,
    ScopeVersionStatus,
)
from tax_risk.persistence.business_entertainment_models import (
    BusinessEntertainmentScopeCompany,
    BusinessEntertainmentScopeVersion as ScopeVersionModel,
)
from tax_risk.persistence.ingest_models import (
    Company,
    CompanyLifecycle,
    IngestBatch,
    IngestBatchStatus,
    IngestError,
    IngestMode,
    SourceRecord,
)
from tax_risk.persistence.repositories import UnitOfWork


UowFactory = Callable[[], UnitOfWork]
SOURCE = "BUSINESS_ENTERTAINMENT_SCOPE_XLSX"
DATASET_CODE = "business_entertainment_company_scope"
SCHEMA_VERSION = "business-entertainment-scope-xlsx-v1"


@dataclass(frozen=True, slots=True)
class ScopeIssue:
    row_number: int
    error_code: str
    message: str
    field: str | None = None
    rejected_value: str | None = None


class BusinessEntertainmentScopeError(Exception):
    error_code = "BUSINESS_ENTERTAINMENT_SCOPE_ERROR"


class ScopeValidationError(BusinessEntertainmentScopeError):
    error_code = "BUSINESS_ENTERTAINMENT_SCOPE_VALIDATION_FAILED"

    def __init__(self, issues: tuple[ScopeIssue, ...], *, batch_id: UUID | None = None) -> None:
        self.issues = issues
        self.batch_id = batch_id
        super().__init__(issues[0].message if issues else "scope validation failed")


class ScopeConflictError(BusinessEntertainmentScopeError):
    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        super().__init__(message)


class ScopeNotReadyError(BusinessEntertainmentScopeError):
    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        super().__init__(message)


class BusinessEntertainmentScopeService:
    def __init__(self, uow_factory: UowFactory) -> None:
        self._uow_factory = uow_factory

    def import_xlsx(
        self,
        *,
        filename: str,
        payload: bytes,
        uploaded_by: str,
    ) -> BusinessEntertainmentScopeVersion:
        filename = _required_filename(filename)
        uploaded_by = _required_identity(uploaded_by, "uploaded_by")
        adapter = BusinessEntertainmentScopeXlsxAdapter(payload)
        try:
            rows = adapter.parse()
        except BusinessEntertainmentScopeWorkbookError as error:
            raise ScopeValidationError(tuple(_adapter_issue(item) for item in error.errors)) from error

        imported_at = datetime.now(timezone.utc)
        source_batch_key = sha256(
            f"{uploaded_by}\0{filename}\0{adapter.checksum}".encode()
        ).hexdigest()
        effective_from = rows[0].effective_from
        effective_to = rows[0].effective_to

        with self._uow_factory() as uow:
            uow.business_entertainment_scope.lock_import(source_batch_key)
            existing = uow.ingest.get_batch_by_source_key(SOURCE, source_batch_key)
            if existing is not None:
                return self._replayed_version(
                    uow,
                    existing,
                    uploaded_by=uploaded_by,
                    checksum=adapter.checksum,
                    filename=filename,
                )

            companies = uow.ingest.lock_companies_shared(row.company_code for row in rows)
            issues = _company_issues(rows, companies)
            batch = IngestBatch(
                source=SOURCE,
                source_batch_key=source_batch_key,
                dataset_code=DATASET_CODE,
                status=(IngestBatchStatus.FAILED if issues else IngestBatchStatus.SUCCEEDED),
                extraction_time=imported_at,
                period=effective_from,
                mode=IngestMode.FULL,
                schema_version=SCHEMA_VERSION,
                payload_ref=filename,
                source_primary_key_definition={
                    "fields": ["company_code"],
                    "uploaded_by": uploaded_by,
                    "source_filename": filename,
                },
                currency="CNY",
                amount_scale=0,
                record_count=len(rows),
                accepted_count=(0 if issues else len(rows)),
                rejected_count=(len(rows) if issues else 0),
                control_total=Decimal("0"),
                checksum=adapter.checksum,
            )
            uow.ingest.add_batch(batch)
            uow.session.flush()
            if issues:
                for issue in issues:
                    uow.ingest.add_error(
                        IngestError(
                            batch_id=batch.id,
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
                uow.commit()
                raise ScopeValidationError(tuple(issues), batch_id=batch.id)

            version = ScopeVersionModel(
                batch_id=batch.id,
                effective_from=effective_from,
                effective_to=effective_to,
                source_file_name=filename,
                file_checksum=adapter.checksum,
                uploader_id=uploaded_by,
                reviewer_id=None,
                status=ScopeVersionStatus.DRAFT,
                approved_at=None,
                published_at=None,
                published_by=None,
            )
            uow.business_entertainment_scope.add_version(version)
            uow.session.flush()

            source_records: list[tuple[BusinessEntertainmentScopeRow, SourceRecord]] = []
            for row in rows:
                company = companies[row.company_code]
                assert company is not None
                source_record = SourceRecord(
                    batch_id=batch.id,
                    source_record_key=row.company_code,
                    company_id=company.id,
                    dataset_code=DATASET_CODE,
                    period=row.effective_from,
                    currency="CNY",
                    amount_scale=0,
                    amount=Decimal("0"),
                    payload={
                        "company_code": row.company_code,
                        "effective_from": row.effective_from.isoformat(),
                        "effective_to": row.effective_to.isoformat(),
                    },
                    lineage={
                        "source_file_name": filename,
                        "source_row_number": row.row_number,
                        "source_checksum": adapter.checksum,
                    },
                    extracted_at=imported_at,
                )
                uow.ingest.add_source_record(source_record)
                source_records.append((row, source_record))
            uow.session.flush()

            for row, source_record in source_records:
                company = companies[row.company_code]
                assert company is not None
                uow.business_entertainment_scope.add_company(
                    BusinessEntertainmentScopeCompany(
                        version_id=version.id,
                        company_id=company.id,
                        source_record_id=source_record.id,
                    )
                )
            uow.session.flush()
            result = _version_view(version)
            uow.commit()
            return result

    @staticmethod
    def _replayed_version(
        uow: UnitOfWork,
        batch: IngestBatch,
        *,
        uploaded_by: str,
        checksum: str,
        filename: str,
    ) -> BusinessEntertainmentScopeVersion:
        if (
            batch.dataset_code != DATASET_CODE
            or batch.checksum != checksum
            or batch.payload_ref != filename
            or batch.source_primary_key_definition.get("uploaded_by") != uploaded_by
            or batch.source_primary_key_definition.get("source_filename") != filename
        ):
            raise ScopeConflictError(
                "BUSINESS_ENTERTAINMENT_SCOPE_IDEMPOTENCY_CONFLICT",
                "the idempotency key already exists with different import metadata",
            )
        if batch.status == IngestBatchStatus.FAILED:
            persisted_errors = uow.ingest.list_errors(batch.id)
            if not persisted_errors:
                raise ScopeConflictError(
                    "BUSINESS_ENTERTAINMENT_SCOPE_LINEAGE_CONFLICT",
                    "failed scope source batch has no persisted validation errors",
                )
            raise ScopeValidationError(
                tuple(
                    ScopeIssue(
                        row_number=error.row_number,
                        error_code=error.error_code,
                        message=error.message,
                        field=error.details.get("field"),
                        rejected_value=error.details.get("rejected_value"),
                    )
                    for error in persisted_errors
                ),
                batch_id=batch.id,
            )
        if batch.status != IngestBatchStatus.SUCCEEDED:
            raise ScopeConflictError(
                "BUSINESS_ENTERTAINMENT_SCOPE_STATE_CONFLICT",
                f"scope import cannot replay from {batch.status.value}",
            )
        version = uow.business_entertainment_scope.version_for_batch(batch.id)
        if version is None:
            raise ScopeConflictError(
                "BUSINESS_ENTERTAINMENT_SCOPE_LINEAGE_CONFLICT",
                "scope source batch does not have a governed version",
            )
        companies = uow.business_entertainment_scope.companies_for_version(version.id)
        if len(companies) != batch.accepted_count:
            raise ScopeConflictError(
                "BUSINESS_ENTERTAINMENT_SCOPE_LINEAGE_CONFLICT",
                "scope source batch does not reconcile to its company rows",
            )
        return _version_view(version)

    def approve(
        self,
        version_id: UUID,
        *,
        reviewed_by: str,
    ) -> BusinessEntertainmentScopeVersion:
        reviewed_by = _required_identity(reviewed_by, "reviewed_by")
        with self._uow_factory() as uow:
            version = uow.business_entertainment_scope.get_version(version_id, for_update=True)
            if version is None:
                raise ScopeNotReadyError(
                    "BUSINESS_ENTERTAINMENT_SCOPE_VERSION_NOT_FOUND",
                    f"scope version {version_id} was not found",
                )
            if version.status != ScopeVersionStatus.DRAFT:
                raise ScopeConflictError(
                    "BUSINESS_ENTERTAINMENT_SCOPE_STATE_CONFLICT",
                    f"scope version cannot be approved from {version.status.value}",
                )
            if reviewed_by == version.uploader_id:
                raise ScopeConflictError(
                    "BUSINESS_ENTERTAINMENT_SCOPE_MAKER_REVIEWER_CONFLICT",
                    "reviewer must be different from the uploader",
                )
            version.status = ScopeVersionStatus.APPROVED
            version.reviewer_id = reviewed_by
            version.approved_at = datetime.now(timezone.utc)
            uow.session.flush()
            result = _version_view(version)
            uow.commit()
            return result

    def publish(
        self,
        version_id: UUID,
        *,
        published_by: str,
    ) -> BusinessEntertainmentScopeVersion:
        published_by = _required_identity(published_by, "published_by")
        with self._uow_factory() as uow:
            uow.business_entertainment_scope.lock_publication()
            version = uow.business_entertainment_scope.get_version(version_id, for_update=True)
            if version is None:
                raise ScopeNotReadyError(
                    "BUSINESS_ENTERTAINMENT_SCOPE_VERSION_NOT_FOUND",
                    f"scope version {version_id} was not found",
                )
            if version.status != ScopeVersionStatus.APPROVED:
                raise ScopeConflictError(
                    "BUSINESS_ENTERTAINMENT_SCOPE_STATE_CONFLICT",
                    f"scope version cannot be published from {version.status.value}",
                )
            if uow.business_entertainment_scope.overlapping_published(version):
                raise ScopeConflictError(
                    "BUSINESS_ENTERTAINMENT_SCOPE_PERIOD_OVERLAP",
                    "published scope periods must not overlap",
                )
            version.status = ScopeVersionStatus.PUBLISHED
            version.published_by = published_by
            version.published_at = datetime.now(timezone.utc)
            uow.session.flush()
            result = _version_view(version)
            uow.commit()
            return result

    def resolve(self, *, effective_on: date) -> BusinessEntertainmentScopeResolution:
        if not isinstance(effective_on, date):
            raise TypeError("effective_on must be a date")
        with self._uow_factory() as uow:
            versions = uow.business_entertainment_scope.published_for_date(effective_on)
            if len(versions) != 1:
                raise ScopeNotReadyError(
                    "BUSINESS_ENTERTAINMENT_SCOPE_NOT_READY",
                    "exactly one published scope version must cover the requested date",
                )
            version = versions[0]
            companies = uow.business_entertainment_scope.companies_for_version(version.id)
            if not companies or any(
                company.lifecycle != CompanyLifecycle.ACTIVE for company in companies
            ):
                raise ScopeNotReadyError(
                    "BUSINESS_ENTERTAINMENT_SCOPE_NOT_READY",
                    "published scope references a missing or inactive company",
                )
            return BusinessEntertainmentScopeResolution(
                version_id=version.id,
                effective_from=version.effective_from,
                effective_to=version.effective_to,
                company_codes=tuple(company.company_code for company in companies),
            )


def _company_issues(
    rows: Iterable[BusinessEntertainmentScopeRow],
    companies: dict[str, Company | None],
) -> list[ScopeIssue]:
    issues: list[ScopeIssue] = []
    for row in rows:
        company = companies.get(row.company_code)
        if company is None:
            issues.append(
                ScopeIssue(
                    row_number=row.row_number,
                    error_code="UNKNOWN_COMPANY",
                    message="company_code is not present in controlled company master",
                    field="company_code",
                    rejected_value=row.company_code,
                )
            )
        elif company.lifecycle != CompanyLifecycle.ACTIVE:
            issues.append(
                ScopeIssue(
                    row_number=row.row_number,
                    error_code="INACTIVE_COMPANY",
                    message="company_code is inactive",
                    field="company_code",
                    rejected_value=row.company_code,
                )
            )
    return sorted(issues, key=lambda issue: (issue.row_number, issue.error_code))


def _adapter_issue(error: BusinessEntertainmentScopeRowError) -> ScopeIssue:
    return ScopeIssue(
        row_number=error.row_number,
        error_code=error.error_code,
        message=error.message,
        field=error.field,
        rejected_value=error.rejected_value,
    )


def _version_view(version: ScopeVersionModel) -> BusinessEntertainmentScopeVersion:
    return BusinessEntertainmentScopeVersion(
        version_id=version.id,
        batch_id=version.batch_id,
        effective_from=version.effective_from,
        effective_to=version.effective_to,
        source_file_name=version.source_file_name,
        file_checksum=version.file_checksum,
        uploader_id=version.uploader_id,
        reviewer_id=version.reviewer_id,
        status=version.status,
        published_at=version.published_at,
    )


def _required_filename(filename: str) -> str:
    if not isinstance(filename, str) or not filename.strip():
        raise ScopeValidationError((ScopeIssue(1, "INVALID_FILENAME", "filename is required"),))
    normalized = PurePath(filename.strip()).name
    if not normalized.lower().endswith(".xlsx"):
        raise ScopeValidationError(
            (ScopeIssue(1, "INVALID_FILENAME", "filename must use the .xlsx extension"),)
        )
    return normalized


def _required_identity(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ScopeValidationError(
            (ScopeIssue(1, "REQUIRED_VALUE", f"{field} is required", field=field),)
        )
    normalized = value.strip()
    if len(normalized) > 256:
        raise ScopeValidationError(
            (ScopeIssue(1, "VALUE_TOO_LONG", f"{field} is too long", field=field),)
        )
    return normalized


__all__ = [
    "BusinessEntertainmentScopeError",
    "BusinessEntertainmentScopeService",
    "ScopeConflictError",
    "ScopeIssue",
    "ScopeNotReadyError",
    "ScopeValidationError",
]
