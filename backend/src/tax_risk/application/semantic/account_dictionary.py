"""Import, approve, publish, and resolve the shared suggested-account dictionary."""

from __future__ import annotations

from collections.abc import Callable
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
from pathlib import PurePath
from uuid import UUID

from sqlalchemy import text

from tax_risk.adapters.ingest.suggested_account_dictionary_xlsx import (
    SuggestedAccountDictionaryXlsxAdapter,
    SuggestedAccountWorkbookError,
)
from tax_risk.domain.semantic.account_dictionary import (
    AccountDictionaryVersionStatus,
    AccountEntryStatus,
    SuggestedAccountDictionaryVersion,
    SuggestedAccountEntry,
)
from tax_risk.persistence.ingest_models import (
    IngestBatch,
    IngestBatchStatus,
    IngestMode,
    SourceRecord,
)
from tax_risk.persistence.repositories import UnitOfWork
from tax_risk.persistence.semantic_models import (
    SuggestedAccountDictionaryVersion as DictionaryVersionModel,
)
from tax_risk.persistence.semantic_models import SuggestedAccountEntry as AccountEntryModel


SOURCE = "SUGGESTED_ACCOUNT_DICTIONARY_XLSX"
DATASET_CODE = "suggested_account_dictionary"
SCHEMA_VERSION = "suggested-account-dictionary-xlsx-v1"
UowFactory = Callable[[], UnitOfWork]


class AccountDictionaryError(Exception):
    error_code = "ACCOUNT_DICTIONARY_ERROR"


class AccountDictionaryValidationError(AccountDictionaryError):
    error_code = "ACCOUNT_DICTIONARY_VALIDATION_FAILED"


class AccountDictionaryConflictError(AccountDictionaryError):
    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        super().__init__(message)


class AccountDictionaryNotReadyError(AccountDictionaryError):
    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        super().__init__(message)


class SuggestedAccountDictionaryService:
    def __init__(self, uow_factory: UowFactory) -> None:
        self._uow_factory = uow_factory

    def import_xlsx(
        self,
        *,
        filename: str,
        payload: bytes,
        uploaded_by: str,
    ) -> SuggestedAccountDictionaryVersion:
        filename = _required(filename, "filename")
        uploaded_by = _required(uploaded_by, "uploaded_by")
        adapter = SuggestedAccountDictionaryXlsxAdapter(payload)
        try:
            rows = adapter.parse()
        except SuggestedAccountWorkbookError as error:
            raise AccountDictionaryValidationError(str(error)) from error
        checksum = adapter.checksum
        source_key = sha256(
            f"{uploaded_by}\0{filename}\0{checksum}".encode()
        ).hexdigest()
        now = datetime.now(timezone.utc)

        with self._uow_factory() as uow:
            uow.session.execute(
                text("SELECT pg_advisory_xact_lock(:namespace, hashtext(:key))"),
                {"namespace": 20260714, "key": source_key},
            )
            existing_batch = uow.ingest.get_batch_by_source_key(SOURCE, source_key)
            if existing_batch is not None:
                existing_version = uow.semantic.get_account_dictionary_by_batch(
                    existing_batch.id
                )
                if existing_version is None:
                    raise AccountDictionaryConflictError(
                        "ACCOUNT_DICTIONARY_LINEAGE_CONFLICT",
                        "existing import batch has no dictionary version",
                    )
                return _version_view(existing_version)
            duplicate_name = uow.semantic.get_account_dictionary_by_name(
                rows[0].dictionary_version
            )
            if duplicate_name is not None:
                raise AccountDictionaryConflictError(
                    "ACCOUNT_DICTIONARY_VERSION_EXISTS",
                    "dictionary_version already exists",
                )

            batch = IngestBatch(
                source=SOURCE,
                source_batch_key=source_key,
                dataset_code=DATASET_CODE,
                status=IngestBatchStatus.SUCCEEDED,
                extraction_time=now,
                period=rows[0].effective_from,
                mode=IngestMode.FULL,
                schema_version=SCHEMA_VERSION,
                payload_ref=filename,
                source_primary_key_definition={
                    "fields": ["dictionary_version", "account_id"],
                    "uploaded_by": uploaded_by,
                },
                currency="CNY",
                amount_scale=0,
                record_count=len(rows),
                accepted_count=len(rows),
                rejected_count=0,
                control_total=Decimal("0"),
                checksum=checksum,
            )
            uow.ingest.add_batch(batch)
            uow.session.flush()
            version = DictionaryVersionModel(
                batch_id=batch.id,
                dictionary_version=rows[0].dictionary_version,
                effective_from=rows[0].effective_from,
                effective_to=rows[0].effective_to,
                checksum=checksum,
                uploaded_by=uploaded_by,
                reviewer_id=None,
                published_by=None,
                status=AccountDictionaryVersionStatus.DRAFT.value,
                approved_at=None,
                published_at=None,
            )
            uow.semantic.add_account_dictionary_version(version)
            uow.session.flush()
            for row in rows:
                source_record = SourceRecord(
                    batch_id=batch.id,
                    source_record_key=f"{row.dictionary_version}|{row.account_id}",
                    company_id=None,
                    dataset_code=DATASET_CODE,
                    period=row.effective_from,
                    currency="CNY",
                    amount_scale=0,
                    amount=Decimal("0"),
                    payload={
                        "dictionary_version": row.dictionary_version,
                        "account_id": row.account_id,
                        "account_code": row.account_code,
                        "account_name": row.account_name,
                        "accounting_classification": row.accounting_classification,
                        "allowed_monitor_types": list(row.allowed_monitor_types),
                        "allowed_labels": list(row.allowed_labels),
                        "effective_from": row.effective_from.isoformat(),
                        "effective_to": row.effective_to.isoformat(),
                        "status": row.status.value,
                    },
                    lineage={
                        "source_file_name": filename,
                        "source_row_number": row.row_number,
                        "source_checksum": checksum,
                    },
                    extracted_at=now,
                )
                uow.ingest.add_source_record(source_record)
                uow.session.flush()
                uow.semantic.add_suggested_account(
                    AccountEntryModel(
                        dictionary_version_id=version.id,
                        source_record_id=source_record.id,
                        account_id=row.account_id,
                        account_code=row.account_code,
                        account_name=row.account_name,
                        accounting_classification=row.accounting_classification,
                        allowed_monitor_types=list(row.allowed_monitor_types),
                        allowed_labels=list(row.allowed_labels),
                        status=row.status.value,
                    )
                )
            uow.session.flush()
            result = _version_view(version)
            uow.commit()
            return result

    def approve(
        self,
        version_id: UUID,
        *,
        reviewed_by: str,
    ) -> SuggestedAccountDictionaryVersion:
        reviewed_by = _required(reviewed_by, "reviewed_by")
        with self._uow_factory() as uow:
            version = uow.semantic.get_account_dictionary_version(version_id, for_update=True)
            if version is None:
                raise AccountDictionaryNotReadyError(
                    "ACCOUNT_DICTIONARY_NOT_FOUND", "dictionary version was not found"
                )
            if version.status != AccountDictionaryVersionStatus.DRAFT.value:
                raise AccountDictionaryConflictError(
                    "ACCOUNT_DICTIONARY_STATE_CONFLICT",
                    f"dictionary cannot be approved from {version.status}",
                )
            if reviewed_by == version.uploaded_by:
                raise AccountDictionaryConflictError(
                    "MAKER_REVIEWER_CONFLICT",
                    "reviewer must be different from the uploader",
                )
            version.status = AccountDictionaryVersionStatus.APPROVED.value
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
    ) -> SuggestedAccountDictionaryVersion:
        published_by = _required(published_by, "published_by")
        with self._uow_factory() as uow:
            version = uow.semantic.get_account_dictionary_version(version_id, for_update=True)
            if version is None:
                raise AccountDictionaryNotReadyError(
                    "ACCOUNT_DICTIONARY_NOT_FOUND", "dictionary version was not found"
                )
            if version.status != AccountDictionaryVersionStatus.APPROVED.value:
                raise AccountDictionaryConflictError(
                    "ACCOUNT_DICTIONARY_STATE_CONFLICT",
                    f"dictionary cannot be published from {version.status}",
                )
            if uow.semantic.overlapping_published_account_dictionaries(version):
                raise AccountDictionaryConflictError(
                    "ACCOUNT_DICTIONARY_PERIOD_OVERLAP",
                    "published account dictionary effective periods must not overlap",
                )
            version.status = AccountDictionaryVersionStatus.PUBLISHED.value
            version.published_by = published_by
            version.published_at = datetime.now(timezone.utc)
            uow.session.flush()
            result = _version_view(version)
            uow.commit()
            return result

    def resolve_account(
        self,
        *,
        dictionary_version: str,
        account_id: str,
        monitor_type: str,
        semantic_label: str,
        effective_on: date,
    ) -> SuggestedAccountEntry:
        with self._uow_factory() as uow:
            version = uow.semantic.get_account_dictionary_by_name(dictionary_version)
            if version is None or version.status != AccountDictionaryVersionStatus.PUBLISHED.value:
                raise AccountDictionaryNotReadyError(
                    "ACCOUNT_DICTIONARY_NOT_PUBLISHED",
                    "only a published account dictionary can be resolved",
                )
            if not version.effective_from <= effective_on <= version.effective_to:
                raise AccountDictionaryNotReadyError(
                    "ACCOUNT_DICTIONARY_OUT_OF_PERIOD",
                    "published account dictionary is not effective on the requested date",
                )
            entry = uow.semantic.get_suggested_account(version.id, account_id)
            if entry is None or entry.status != AccountEntryStatus.ACTIVE.value:
                raise AccountDictionaryNotReadyError(
                    "ACCOUNT_ID_NOT_VALID",
                    "suggested account ID is not valid in the published dictionary",
                )
            if (
                monitor_type not in entry.allowed_monitor_types
                or semantic_label not in entry.allowed_labels
            ):
                raise AccountDictionaryNotReadyError(
                    "ACCOUNT_ID_NOT_COMPATIBLE",
                    "suggested account is not compatible with the monitor and label",
                )
            return _entry_view(entry)


def _required(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AccountDictionaryValidationError(f"{field} is required")
    normalized = value.strip()
    if field == "filename" and PurePath(normalized).name != normalized:
        raise AccountDictionaryValidationError("filename must not contain a path")
    return normalized


def _version_view(model: DictionaryVersionModel) -> SuggestedAccountDictionaryVersion:
    return SuggestedAccountDictionaryVersion(
        version_id=model.id,
        batch_id=model.batch_id,
        dictionary_version=model.dictionary_version,
        effective_from=model.effective_from,
        effective_to=model.effective_to,
        checksum=model.checksum,
        uploaded_by=model.uploaded_by,
        reviewer_id=model.reviewer_id,
        published_by=model.published_by,
        status=AccountDictionaryVersionStatus(model.status),
        approved_at=model.approved_at,
        published_at=model.published_at,
    )


def _entry_view(model: AccountEntryModel) -> SuggestedAccountEntry:
    return SuggestedAccountEntry(
        account_id=model.account_id,
        account_code=model.account_code,
        account_name=model.account_name,
        accounting_classification=model.accounting_classification,
        allowed_monitor_types=tuple(model.allowed_monitor_types),
        allowed_labels=tuple(model.allowed_labels),
        status=AccountEntryStatus(model.status),
    )


__all__ = [
    "AccountDictionaryConflictError",
    "AccountDictionaryNotReadyError",
    "AccountDictionaryValidationError",
    "SuggestedAccountDictionaryService",
]
