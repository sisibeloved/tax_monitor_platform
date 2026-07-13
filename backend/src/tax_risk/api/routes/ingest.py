from __future__ import annotations

from threading import BoundedSemaphore
from typing import BinaryIO, Never, cast
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile, status

from tax_risk.api.schemas import IngestBatchCreate, IngestBatchResponse
from tax_risk.api.dependencies import require_group_tax
from tax_risk.application.ingest import (
    BatchMetadata,
    BatchNotFoundError,
    BatchStateConflictError,
    FileSchemaError,
    IdempotencyMetadataConflictError,
    IngestApplicationError,
    IngestProcessingError,
    IngestService,
    TerminalBatchFileConflictError,
    AdapterFactory,
    UowFactory,
)


router = APIRouter(
    prefix="/api/v1/ingest-batches",
    tags=["ingest"],
    dependencies=[Depends(require_group_tax)],
)


def get_ingest_service(request: Request) -> IngestService:
    return IngestService(
        cast(UowFactory, request.app.state.uow_factory),
        cast(AdapterFactory, request.app.state.adapter_factory),
    )


@router.post("", response_model=IngestBatchResponse, status_code=status.HTTP_201_CREATED)
def create_ingest_batch(
    request: IngestBatchCreate,
    response: Response,
    service: IngestService = Depends(get_ingest_service),
) -> object:
    metadata = BatchMetadata(
        source=request.source,
        source_batch_key=request.source_batch_key,
        dataset_code=request.dataset_code,
        extraction_time=request.extraction_time,
        period=request.period,
        mode=request.mode,
        schema_version=request.schema_version,
        currency=request.currency,
        amount_scale=request.amount_scale,
        source_primary_key_definition=request.source_primary_key_definition,
    )
    try:
        result = service.create_batch(metadata)
    except IngestApplicationError as error:
        _raise_http(error)
    if not result.created:
        response.status_code = status.HTTP_200_OK
    return result.batch


@router.post("/{batch_id}/files", response_model=IngestBatchResponse)
def upload_ingest_file(
    batch_id: UUID,
    request: Request,
    file: UploadFile = File(...),
    service: IngestService = Depends(get_ingest_service),
) -> object:
    semaphore = cast(BoundedSemaphore, request.app.state.ingest_upload_semaphore)
    if not semaphore.acquire(blocking=False):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "INGEST_CAPACITY_EXCEEDED",
                "message": "ingest upload capacity is currently exhausted",
            },
        )
    try:
        try:
            payload = _read_upload_limited(
                file.file,
                cast(int, request.app.state.ingest_max_upload_bytes),
            )
        except FileTooLargeError as error:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail={"code": "FILE_TOO_LARGE", "message": str(error)},
            ) from error
        try:
            return service.ingest_csv(batch_id, file.filename or "upload.csv", payload)
        except IngestApplicationError as error:
            _raise_http(error)
    finally:
        semaphore.release()


@router.get("/{batch_id}", response_model=IngestBatchResponse)
def get_ingest_batch(
    batch_id: UUID,
    service: IngestService = Depends(get_ingest_service),
) -> object:
    try:
        return service.get_batch(batch_id)
    except IngestApplicationError as error:
        _raise_http(error)


def _raise_http(error: IngestApplicationError) -> Never:
    if isinstance(error, BatchNotFoundError):
        status_code = status.HTTP_404_NOT_FOUND
    elif isinstance(error, FileSchemaError):
        status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    elif isinstance(
        error,
        (
            IdempotencyMetadataConflictError,
            TerminalBatchFileConflictError,
            BatchStateConflictError,
        ),
    ):
        status_code = status.HTTP_409_CONFLICT
    elif isinstance(error, IngestProcessingError):
        status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    else:
        status_code = status.HTTP_400_BAD_REQUEST

    detail: dict[str, object] = {
        "code": error.error_code,
        "message": str(error),
    }
    if isinstance(error, FileSchemaError):
        detail["missing_columns"] = list(error.missing_columns)
        detail["extra_columns"] = list(error.extra_columns)
    raise HTTPException(status_code=status_code, detail=detail)


class FileTooLargeError(ValueError):
    pass


def _read_upload_limited(stream: BinaryIO, maximum_bytes: int) -> bytes:
    payload = bytearray()
    while chunk := stream.read(64 * 1024):
        if len(payload) + len(chunk) > maximum_bytes:
            raise FileTooLargeError(f"uploaded file exceeds {maximum_bytes} bytes")
        payload.extend(chunk)
    return bytes(payload)


__all__ = ["router"]
