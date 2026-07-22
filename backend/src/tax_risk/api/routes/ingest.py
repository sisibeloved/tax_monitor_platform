from __future__ import annotations

from threading import BoundedSemaphore
from typing import BinaryIO, Never, cast
from uuid import UUID

from fastapi import APIRouter, Depends, File, HTTPException, Request, Response, UploadFile, status

from tax_risk.adapters.ingest.dgc_sap_profit import (
    DgcSapProfitError,
    DgcSapProfitFieldMap,
    DgcSapProfitMetricMap,
)
from tax_risk.adapters.ingest.dgc_sap_account_balance import (
    DGC_SAP_ACCOUNT_BALANCE_TABLE_NAME,
    DgcSapAccountBalanceError,
)
from tax_risk.adapters.ingest.dgc_hesi_no_invoice import (
    DgcHesiInvoiceFieldMap,
    DgcHesiNoInvoiceError,
    DgcHesiReimbursementFieldMap,
)
from tax_risk.adapters.ingest.dgc_sap_dividend_detail import (
    DGC_SETTLEMENT_ACCOUNT_DETAIL_TABLE_NAME,
    DgcSapDividendDetailError,
)
from tax_risk.adapters.ingest.dgc_sap_trial_balance import DgcSapTrialBalanceError
from tax_risk.api.schemas import (
    DgcHesiNoInvoiceImportRequest,
    DgcSapAccountBalanceImportRequest,
    DgcSapDividendDetailImportRequest,
    DgcSapProfitImportRequest,
    DgcSapTrialBalanceImportRequest,
    IngestBatchCreate,
    IngestBatchResponse,
)
from tax_risk.application.dgc_sap_account_balance import (
    DgcSapAccountBalanceImportCommand,
    DgcSapAccountBalanceImportService,
    DgcSapAccountBalanceSource,
)
from tax_risk.application.dgc_hesi_no_invoice import (
    DgcHesiNoInvoiceImportCommand,
    DgcHesiNoInvoiceImportService,
)
from tax_risk.application.dgc_hesi_invoice import DgcHesiInvoiceSource
from tax_risk.application.dgc_hesi_reimbursement import DgcHesiReimbursementSource
from tax_risk.application.dgc_sap_dividend_detail import (
    DgcSapDividendDetailImportCommand,
    DgcSapDividendDetailImportService,
    DgcSapDividendDetailSource,
)
from tax_risk.api.dependencies import require_group_tax
from tax_risk.application.dgc_sap_profit import (
    DgcSapProfitImportCommand,
    DgcSapProfitImportService,
    DgcSapProfitSource,
)
from tax_risk.application.dgc_sap_trial_balance import (
    DgcSapTrialBalanceImportCommand,
    DgcSapTrialBalanceImportService,
    DgcSapTrialBalanceSource,
)
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


def get_dgc_sap_profit_import_service(request: Request) -> DgcSapProfitImportService:
    source = getattr(request.app.state, "dgc_sap_profit_client", None)
    if source is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "DGC_SAP_PROFIT_DISABLED",
                "message": "DGC SAP profit-statement import is not configured",
            },
        )
    return DgcSapProfitImportService(
        get_ingest_service(request),
        cast(DgcSapProfitSource, source),
        cast(DgcSapProfitFieldMap, request.app.state.dgc_sap_profit_field_map),
        cast(DgcSapProfitMetricMap, request.app.state.dgc_sap_profit_metric_map),
        cast(str, request.app.state.dgc_sap_profit_ledger),
    )


def get_dgc_sap_dividend_detail_import_service(
    request: Request,
) -> DgcSapDividendDetailImportService:
    source = getattr(request.app.state, "dgc_sap_dividend_detail_client", None)
    if source is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "DGC_SAP_DIVIDEND_DETAIL_DISABLED",
                "message": (
                    f"DGC {DGC_SETTLEMENT_ACCOUNT_DETAIL_TABLE_NAME} import is not configured"
                ),
            },
        )
    return DgcSapDividendDetailImportService(
        get_ingest_service(request),
        cast(DgcSapDividendDetailSource, source),
    )


def get_dgc_sap_trial_balance_import_service(
    request: Request,
) -> DgcSapTrialBalanceImportService:
    source = getattr(request.app.state, "dgc_sap_trial_balance_client", None)
    if source is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "DGC_SAP_TRIAL_BALANCE_DISABLED",
                "message": "DGC SAP trial-balance import is not configured",
            },
        )
    return DgcSapTrialBalanceImportService(
        get_ingest_service(request),
        cast(DgcSapTrialBalanceSource, source),
    )


def get_dgc_sap_account_balance_import_service(
    request: Request,
) -> DgcSapAccountBalanceImportService:
    source = getattr(request.app.state, "dgc_sap_account_balance_client", None)
    if source is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "DGC_SAP_ACCOUNT_BALANCE_DISABLED",
                "message": (
                    f"DGC {DGC_SAP_ACCOUNT_BALANCE_TABLE_NAME} import is not configured"
                ),
            },
        )
    return DgcSapAccountBalanceImportService(
        get_ingest_service(request),
        cast(DgcSapAccountBalanceSource, source),
    )


def get_dgc_hesi_no_invoice_import_service(
    request: Request,
) -> DgcHesiNoInvoiceImportService:
    reimbursement_source = getattr(request.app.state, "dgc_hesi_reimbursement_client", None)
    invoice_source = getattr(request.app.state, "dgc_hesi_invoice_client", None)
    if reimbursement_source is None or invoice_source is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "DGC_HESI_NO_INVOICE_DISABLED",
                "message": "both Hesi reimbursement and invoice sources must be configured",
            },
        )
    return DgcHesiNoInvoiceImportService(
        get_ingest_service(request),
        cast(DgcHesiReimbursementSource, reimbursement_source),
        cast(DgcHesiInvoiceSource, invoice_source),
        cast(
            DgcHesiReimbursementFieldMap,
            request.app.state.dgc_hesi_reimbursement_field_map,
        ),
        cast(DgcHesiInvoiceFieldMap, request.app.state.dgc_hesi_invoice_field_map),
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


@router.post(
    "/dgc-sap-profit",
    response_model=IngestBatchResponse,
    status_code=status.HTTP_201_CREATED,
)
def import_dgc_sap_profit(
    payload: DgcSapProfitImportRequest,
    request: Request,
    response: Response,
    service: DgcSapProfitImportService = Depends(get_dgc_sap_profit_import_service),
) -> object:
    semaphore = cast(BoundedSemaphore, request.app.state.ingest_upload_semaphore)
    if not semaphore.acquire(blocking=False):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "INGEST_CAPACITY_EXCEEDED",
                "message": "ingest capacity is currently exhausted",
            },
        )
    try:
        try:
            result = service.import_profit_statement(
                DgcSapProfitImportCommand(
                    source_batch_key=payload.source_batch_key,
                    extraction_time=payload.extraction_time,
                    gjahr=payload.gjahr,
                    monat=payload.monat,
                    bukrs=payload.bukrs,
                    mode=payload.mode,
                    schema_version=payload.schema_version,
                    currency=payload.currency,
                    amount_scale=payload.amount_scale,
                )
            )
        except DgcSapProfitError as error:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    "code": error.error_code,
                    "message": str(error),
                },
            ) from error
        except IngestApplicationError as error:
            _raise_http(error)
        if not result.created:
            response.status_code = status.HTTP_200_OK
        return result.batch
    finally:
        semaphore.release()


@router.post(
    "/dgc-sap-trial-balance",
    response_model=IngestBatchResponse,
    status_code=status.HTTP_201_CREATED,
)
def import_dgc_sap_trial_balance(
    payload: DgcSapTrialBalanceImportRequest,
    request: Request,
    response: Response,
    service: DgcSapTrialBalanceImportService = Depends(
        get_dgc_sap_trial_balance_import_service
    ),
) -> object:
    semaphore = cast(BoundedSemaphore, request.app.state.ingest_upload_semaphore)
    if not semaphore.acquire(blocking=False):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "INGEST_CAPACITY_EXCEEDED",
                "message": "ingest capacity is currently exhausted",
            },
        )
    try:
        try:
            result = service.import_current_income_tax(
                DgcSapTrialBalanceImportCommand(
                    source_batch_key=payload.source_batch_key,
                    extraction_time=payload.extraction_time,
                    company_code=payload.company_code,
                    fiscal_year=payload.fiscal_year,
                    through_period=payload.through_period,
                    mode=payload.mode,
                    schema_version=payload.schema_version,
                    currency=payload.currency,
                    amount_scale=payload.amount_scale,
                )
            )
        except (DgcSapProfitError, DgcSapTrialBalanceError) as error:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"code": error.error_code, "message": str(error)},
            ) from error
        except IngestApplicationError as error:
            _raise_http(error)
        if not result.created:
            response.status_code = status.HTTP_200_OK
        return result.batch
    finally:
        semaphore.release()


@router.post(
    "/dgc-sap-account-balance",
    response_model=IngestBatchResponse,
    status_code=status.HTTP_201_CREATED,
)
def import_dgc_sap_account_balance(
    payload: DgcSapAccountBalanceImportRequest,
    request: Request,
    response: Response,
    service: DgcSapAccountBalanceImportService = Depends(
        get_dgc_sap_account_balance_import_service
    ),
) -> object:
    semaphore = cast(BoundedSemaphore, request.app.state.ingest_upload_semaphore)
    if not semaphore.acquire(blocking=False):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "INGEST_CAPACITY_EXCEEDED",
                "message": "ingest capacity is currently exhausted",
            },
        )
    try:
        try:
            result = service.import_quarterly_balances(
                DgcSapAccountBalanceImportCommand(
                    source_batch_key=payload.source_batch_key,
                    extraction_time=payload.extraction_time,
                    company_code=payload.company_code,
                    fiscal_year=payload.fiscal_year,
                    fiscal_period=payload.fiscal_period,
                    mode=payload.mode,
                    schema_version=payload.schema_version,
                    currency=payload.currency,
                    amount_scale=payload.amount_scale,
                )
            )
        except (DgcSapProfitError, DgcSapAccountBalanceError) as error:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"code": error.error_code, "message": str(error)},
            ) from error
        except IngestApplicationError as error:
            _raise_http(error)
        if not result.created:
            response.status_code = status.HTTP_200_OK
        return result.batch
    finally:
        semaphore.release()


@router.post(
    "/dgc-hesi-no-invoice",
    response_model=IngestBatchResponse,
    status_code=status.HTTP_201_CREATED,
)
def import_dgc_hesi_no_invoice(
    payload: DgcHesiNoInvoiceImportRequest,
    request: Request,
    response: Response,
    service: DgcHesiNoInvoiceImportService = Depends(
        get_dgc_hesi_no_invoice_import_service
    ),
) -> object:
    semaphore = cast(BoundedSemaphore, request.app.state.ingest_upload_semaphore)
    if not semaphore.acquire(blocking=False):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "INGEST_CAPACITY_EXCEEDED",
                "message": "ingest capacity is currently exhausted",
            },
        )
    try:
        try:
            result = service.import_quarterly_metric(
                DgcHesiNoInvoiceImportCommand(
                    source_batch_key=payload.source_batch_key,
                    extraction_time=payload.extraction_time,
                    company_code=payload.company_code,
                    fiscal_year=payload.fiscal_year,
                    fiscal_period=payload.fiscal_period,
                    mode=payload.mode,
                    schema_version=payload.schema_version,
                    currency=payload.currency,
                    amount_scale=payload.amount_scale,
                )
            )
        except (DgcSapProfitError, DgcHesiNoInvoiceError) as error:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={"code": error.error_code, "message": str(error)},
            ) from error
        except IngestApplicationError as error:
            _raise_http(error)
        if not result.created:
            response.status_code = status.HTTP_200_OK
        return result.batch
    finally:
        semaphore.release()


@router.post(
    "/dgc-sap-dividend-detail",
    response_model=IngestBatchResponse,
    status_code=status.HTTP_201_CREATED,
)
def import_dgc_sap_dividend_detail(
    payload: DgcSapDividendDetailImportRequest,
    request: Request,
    response: Response,
    service: DgcSapDividendDetailImportService = Depends(
        get_dgc_sap_dividend_detail_import_service
    ),
) -> object:
    semaphore = cast(BoundedSemaphore, request.app.state.ingest_upload_semaphore)
    if not semaphore.acquire(blocking=False):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "code": "INGEST_CAPACITY_EXCEEDED",
                "message": "ingest capacity is currently exhausted",
            },
        )
    try:
        try:
            result = service.import_dividend_detail(
                DgcSapDividendDetailImportCommand(
                    source_batch_key=payload.source_batch_key,
                    extraction_time=payload.extraction_time,
                    company=payload.company,
                    fiscal_year=payload.fiscal_year,
                    through_period=payload.through_period,
                    mode=payload.mode,
                    schema_version=payload.schema_version,
                    currency=payload.currency,
                    amount_scale=payload.amount_scale,
                )
            )
        except (DgcSapProfitError, DgcSapDividendDetailError) as error:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    "code": error.error_code,
                    "message": str(error),
                },
            ) from error
        except IngestApplicationError as error:
            _raise_http(error)
        if not result.created:
            response.status_code = status.HTTP_200_OK
        return result.batch
    finally:
        semaphore.release()


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
