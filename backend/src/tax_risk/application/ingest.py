from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from hashlib import sha256
import logging
from typing import Any
from uuid import UUID

from sqlalchemy.exc import IntegrityError

from tax_risk.adapters.ingest.base import BulkFileAdapter, RowError
from tax_risk.adapters.ingest.csv_adapter import CSVAdapter, HeaderValidationError
from tax_risk.application.ingest_processors import (
    CompanyMasterProcessor,
    FinancialProcessor,
)
from tax_risk.persistence.ingest_models import (
    IngestBatch,
    IngestBatchStatus,
    IngestError,
    IngestMode,
)
from tax_risk.persistence.repositories import UnitOfWork


UowFactory = Callable[[], UnitOfWork]
AdapterFactory = Callable[[bytes, str], BulkFileAdapter]
_TERMINAL_STATUSES = {
    IngestBatchStatus.SUCCEEDED,
    IngestBatchStatus.PARTIAL,
    IngestBatchStatus.FAILED,
}
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BatchMetadata:
    source: str
    source_batch_key: str
    dataset_code: str
    extraction_time: datetime
    period: date
    mode: IngestMode
    schema_version: str
    currency: str
    amount_scale: int
    source_primary_key_definition: dict[str, Any]


@dataclass(frozen=True, slots=True)
class BatchErrorView:
    row_number: int
    error_code: str
    message: str
    details: dict[str, Any]
    retryable: bool


@dataclass(frozen=True, slots=True)
class BatchView:
    id: UUID
    source: str
    source_batch_key: str
    dataset_code: str
    status: IngestBatchStatus
    extraction_time: datetime
    period: date
    mode: IngestMode
    schema_version: str
    payload_ref: str | None
    source_primary_key_definition: dict[str, Any]
    currency: str
    amount_scale: int
    record_count: int
    accepted_count: int
    rejected_count: int
    control_total: Decimal
    checksum: str
    errors: tuple[BatchErrorView, ...]
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True, slots=True)
class CreateBatchResult:
    batch: BatchView
    created: bool


class IngestApplicationError(Exception):
    error_code = "INGEST_ERROR"


class BatchNotFoundError(IngestApplicationError):
    error_code = "BATCH_NOT_FOUND"


class IdempotencyMetadataConflictError(IngestApplicationError):
    error_code = "IDEMPOTENCY_METADATA_CONFLICT"


class TerminalBatchFileConflictError(IngestApplicationError):
    error_code = "TERMINAL_BATCH_FILE_CONFLICT"


class BatchStateConflictError(IngestApplicationError):
    error_code = "BATCH_STATE_CONFLICT"


class FileSchemaError(IngestApplicationError):
    def __init__(self, header_error: HeaderValidationError) -> None:
        super().__init__(header_error.message)
        self.error_code = header_error.error_code
        self.missing_columns = header_error.missing_columns
        self.extra_columns = header_error.extra_columns


class IngestProcessingError(IngestApplicationError):
    error_code = "INGEST_PROCESSING_FAILED"


def create_csv_adapter(payload: bytes, dataset_code: str) -> BulkFileAdapter:
    return CSVAdapter(payload, dataset_code=dataset_code)


class IngestService:
    def __init__(
        self,
        uow_factory: UowFactory,
        adapter_factory: AdapterFactory = create_csv_adapter,
    ) -> None:
        self._uow_factory = uow_factory
        self._adapter_factory = adapter_factory

    def create_batch(self, metadata: BatchMetadata) -> CreateBatchResult:
        with self._uow_factory() as uow:
            existing = uow.ingest.get_batch_by_source_key(
                metadata.source,
                metadata.source_batch_key,
            )
            if existing is not None:
                self._assert_metadata_matches(existing, metadata)
                return CreateBatchResult(self._view(uow, existing), created=False)

            batch = IngestBatch(
                source=metadata.source,
                source_batch_key=metadata.source_batch_key,
                dataset_code=metadata.dataset_code,
                status=IngestBatchStatus.RECEIVED,
                extraction_time=metadata.extraction_time,
                period=metadata.period,
                mode=metadata.mode,
                schema_version=metadata.schema_version,
                source_primary_key_definition=metadata.source_primary_key_definition,
                currency=metadata.currency,
                amount_scale=metadata.amount_scale,
                record_count=0,
                accepted_count=0,
                rejected_count=0,
                control_total=Decimal("0"),
                checksum="0" * 64,
            )
            uow.ingest.add_batch(batch)
            try:
                uow.commit()
            except IntegrityError:
                # A concurrent request may have won the canonical unique key.
                uow.rollback()
                concurrent = uow.ingest.get_batch_by_source_key(
                    metadata.source,
                    metadata.source_batch_key,
                )
                if concurrent is None:
                    raise
                self._assert_metadata_matches(concurrent, metadata)
                return CreateBatchResult(self._view(uow, concurrent), created=False)
            uow.session.refresh(batch)
            return CreateBatchResult(self._view(uow, batch), created=True)

    def get_batch(self, batch_id: UUID) -> BatchView:
        with self._uow_factory() as uow:
            batch = uow.ingest.get_batch(batch_id)
            if batch is None:
                raise BatchNotFoundError(f"ingest batch {batch_id} was not found")
            return self._view(uow, batch)

    def ingest_csv(self, batch_id: UUID, filename: str, payload: bytes) -> BatchView:
        checksum = sha256(payload).hexdigest()
        try:
            return self._ingest_csv_transaction(batch_id, filename, payload, checksum)
        except IngestApplicationError:
            raise
        except Exception as error:
            logger.exception(
                "ingest_processing_failed",
                extra={"event": "ingest_processing_failed", "batch_id": str(batch_id)},
            )
            self._audit_processing_failure(batch_id, filename, checksum)
            raise IngestProcessingError("ingest file processing failed") from error

    def _ingest_csv_transaction(
        self,
        batch_id: UUID,
        filename: str,
        payload: bytes,
        checksum: str,
    ) -> BatchView:
        with self._uow_factory() as uow:
            batch = uow.ingest.get_batch(batch_id, for_update=True)
            if batch is None:
                raise BatchNotFoundError(f"ingest batch {batch_id} was not found")
            if batch.status in _TERMINAL_STATUSES:
                if batch.checksum == checksum:
                    return self._view(uow, batch)
                raise TerminalBatchFileConflictError(
                    f"batch {batch_id} is terminal and already owns a different file"
                )
            if batch.status != IngestBatchStatus.RECEIVED:
                raise BatchStateConflictError(
                    f"batch {batch_id} cannot accept a file while {batch.status.value}"
                )

            adapter = self._adapter_factory(payload, batch.dataset_code)
            if adapter.checksum != checksum:
                raise RuntimeError("adapter checksum does not match the uploaded file")
            batch.status = IngestBatchStatus.VALIDATING
            batch.payload_ref = filename
            batch.checksum = checksum
            try:
                adapter.validate_header()
            except HeaderValidationError as error:
                batch.status = IngestBatchStatus.FAILED
                uow.ingest.add_error(
                    IngestError(
                        batch_id=batch.id,
                        row_number=1,
                        error_code=error.error_code,
                        message=error.message,
                        details={
                            "missing_columns": list(error.missing_columns),
                            "extra_columns": list(error.extra_columns),
                        },
                        retryable=False,
                    )
                )
                uow.commit()
                raise FileSchemaError(error) from error

            processor = (
                CompanyMasterProcessor()
                if batch.dataset_code == "company_master"
                else FinancialProcessor()
            )
            result = processor.process(
                adapter.iter_rows(),
                uow=uow,
                batch=batch,
                checksum=checksum,
            )
            errors = list(result.errors)
            if result.record_count == 0:
                errors.append(RowError(1, "EMPTY_FILE", "file contains no data rows"))
            for row_error in errors:
                self._add_error(uow, batch.id, row_error)
            for source_record in result.source_records:
                uow.ingest.add_source_record(source_record)

            batch.record_count = result.record_count
            batch.accepted_count = result.accepted_count
            batch.rejected_count = result.rejected_count
            batch.control_total = result.control_total
            batch.status = _terminal_status(result.accepted_count, result.rejected_count)
            uow.commit()
            uow.session.refresh(batch)
            return self._view(uow, batch)

    def _audit_processing_failure(
        self,
        batch_id: UUID,
        filename: str,
        checksum: str,
    ) -> None:
        try:
            with self._uow_factory() as uow:
                batch = uow.ingest.get_batch(batch_id, for_update=True)
                if batch is None or batch.status in _TERMINAL_STATUSES:
                    return
                batch.status = IngestBatchStatus.FAILED
                batch.payload_ref = filename
                batch.checksum = checksum
                batch.record_count = 0
                batch.accepted_count = 0
                batch.rejected_count = 0
                batch.control_total = Decimal("0")
                uow.ingest.add_error(
                    IngestError(
                        batch_id=batch.id,
                        row_number=1,
                        error_code="INGEST_PROCESSING_FAILED",
                        message="ingest file processing failed",
                        details={},
                        retryable=False,
                    )
                )
                uow.commit()
        except Exception:
            logger.exception(
                "ingest_processing_audit_failed",
                extra={
                    "event": "ingest_processing_audit_failed",
                    "batch_id": str(batch_id),
                },
            )
            return

    @staticmethod
    def _assert_metadata_matches(batch: IngestBatch, metadata: BatchMetadata) -> None:
        persisted = (
            batch.dataset_code,
            batch.extraction_time,
            batch.period,
            batch.mode,
            batch.schema_version,
            batch.currency,
            batch.amount_scale,
            batch.source_primary_key_definition,
        )
        requested = (
            metadata.dataset_code,
            metadata.extraction_time,
            metadata.period,
            metadata.mode,
            metadata.schema_version,
            metadata.currency,
            metadata.amount_scale,
            metadata.source_primary_key_definition,
        )
        if persisted != requested:
            raise IdempotencyMetadataConflictError(
                "source and source_batch_key already exist with different metadata"
            )

    @staticmethod
    def _view(uow: UnitOfWork, batch: IngestBatch) -> BatchView:
        errors = tuple(
            BatchErrorView(
                row_number=error.row_number,
                error_code=error.error_code,
                message=error.message,
                details=error.details,
                retryable=error.retryable,
            )
            for error in uow.ingest.list_errors(batch.id)
        )
        return BatchView(
            id=batch.id,
            source=batch.source,
            source_batch_key=batch.source_batch_key,
            dataset_code=batch.dataset_code,
            status=batch.status,
            extraction_time=batch.extraction_time,
            period=batch.period,
            mode=batch.mode,
            schema_version=batch.schema_version,
            payload_ref=batch.payload_ref,
            source_primary_key_definition=batch.source_primary_key_definition,
            currency=batch.currency,
            amount_scale=batch.amount_scale,
            record_count=batch.record_count,
            accepted_count=batch.accepted_count,
            rejected_count=batch.rejected_count,
            control_total=batch.control_total,
            checksum=batch.checksum,
            errors=errors,
            created_at=batch.created_at,
            updated_at=batch.updated_at,
        )

    @staticmethod
    def _add_error(uow: UnitOfWork, batch_id: UUID, error: RowError) -> None:
        details: dict[str, Any] = {}
        if error.field is not None:
            details["field"] = error.field
        if error.rejected_value is not None:
            details["rejected_value"] = error.rejected_value
        uow.ingest.add_error(
            IngestError(
                batch_id=batch_id,
                row_number=error.row_number,
                error_code=error.error_code,
                message=error.message,
                details=details,
                retryable=False,
            )
        )


def _terminal_status(accepted_count: int, rejected_count: int) -> IngestBatchStatus:
    if accepted_count == 0:
        return IngestBatchStatus.FAILED
    if rejected_count == 0:
        return IngestBatchStatus.SUCCEEDED
    return IngestBatchStatus.PARTIAL


__all__ = [
    "AdapterFactory",
    "BatchMetadata",
    "BatchNotFoundError",
    "BatchStateConflictError",
    "BatchView",
    "CreateBatchResult",
    "FileSchemaError",
    "IdempotencyMetadataConflictError",
    "IngestService",
    "IngestProcessingError",
    "TerminalBatchFileConflictError",
    "UowFactory",
]
