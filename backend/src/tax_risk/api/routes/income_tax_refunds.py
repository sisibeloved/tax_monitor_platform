"""Import, execute, and report deterministic income-tax refund scans."""

from __future__ import annotations

from collections.abc import Callable
from hashlib import sha256
from threading import BoundedSemaphore
from typing import Annotated, BinaryIO, Never, cast

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)

from tax_risk.adapters.ingest.income_tax_refund_xlsx import (
    IncomeTaxRefundWorkbookError,
    IncomeTaxRefundXlsxAdapter,
)
from tax_risk.adapters.ingest.tax_master_xlsx import XlsxResourceLimits
from tax_risk.api.dependencies import company_scope, require_group_tax, require_reader
from tax_risk.api.income_tax_refund_schemas import (
    IncomeTaxRefundImportResponse,
    IncomeTaxRefundSapEvidenceImportRequest,
    IncomeTaxRefundSapEvidenceResponse,
    IncomeTaxRefundScanItemResponse,
    IncomeTaxRefundScanRequest,
    IncomeTaxRefundScanResponse,
    IncomeTaxRefundTargetImportRequest,
)
from tax_risk.application.income_tax_refunds import (
    IncomeTaxRefundService,
    IncomeTaxRefundServiceError,
    IncomeTaxRefundSummaryView,
    IncomeTaxRefundTargetDraft,
    SapRefundEvidenceDraft,
    SapRefundLineDraft,
)
from tax_risk.persistence.repositories import UnitOfWork
from tax_risk.security.principal import Principal


router = APIRouter(
    prefix="/api/v1/income-tax-refunds",
    tags=["income-tax-refunds"],
)


def _service(request: Request) -> IncomeTaxRefundService:
    factory = cast(Callable[[], UnitOfWork], request.app.state.uow_factory)
    return IncomeTaxRefundService(factory)


@router.post(
    "/targets",
    response_model=IncomeTaxRefundImportResponse,
    status_code=status.HTTP_201_CREATED,
)
def import_targets(
    body: IncomeTaxRefundTargetImportRequest,
    request: Request,
    _principal: Annotated[Principal, Depends(require_group_tax)],
) -> IncomeTaxRefundImportResponse:
    try:
        summary = _service(request).import_targets(
            refund_tax_year=body.refund_tax_year,
            source_version=body.source_version,
            drafts=tuple(
                IncomeTaxRefundTargetDraft(
                    company_code=item.company_code,
                    source_record_key=item.source_record_key,
                    expected_refund_amount=item.expected_refund_amount,
                    raw_expected_refund_amount=item.raw_expected_refund_amount,
                    currency=item.currency,
                    amount_scale=item.amount_scale,
                    received_in_source=item.received_in_source,
                )
                for item in body.items
            ),
        )
    except IncomeTaxRefundServiceError as error:
        _raise_service_error(error)
    request.state.audit_row_count = summary.accepted_count + summary.replayed_count
    return IncomeTaxRefundImportResponse(
        source_version=summary.source_version,
        accepted_count=summary.accepted_count,
        replayed_count=summary.replayed_count,
    )


@router.post(
    "/targets/xlsx",
    response_model=IncomeTaxRefundImportResponse,
    status_code=status.HTTP_201_CREATED,
)
def import_targets_xlsx(
    request: Request,
    _principal: Annotated[Principal, Depends(require_group_tax)],
    refund_tax_year: Annotated[int, Query(ge=2000, le=9998)],
    file: UploadFile = File(...),
    source_version: Annotated[str | None, Query(max_length=128)] = None,
    currency: Annotated[str, Query(pattern=r"^[A-Z]{3}$")] = "CNY",
    amount_scale: Annotated[int, Query(ge=0, le=12)] = 2,
) -> IncomeTaxRefundImportResponse:
    semaphore = cast(BoundedSemaphore, request.app.state.ingest_upload_semaphore)
    if not semaphore.acquire(blocking=False):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"code": "INGEST_CAPACITY_EXCEEDED"},
        )
    try:
        maximum = cast(int, request.app.state.ingest_max_upload_bytes)
        try:
            payload = _read_upload_limited(file.file, maximum)
        except _FileTooLargeError as error:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail={"code": "FILE_TOO_LARGE", "message": str(error)},
            ) from error
        try:
            rows = IncomeTaxRefundXlsxAdapter(
                payload,
                refund_tax_year=refund_tax_year,
                currency=currency,
                amount_scale=amount_scale,
                max_upload_bytes=maximum,
                limits=cast(XlsxResourceLimits, request.app.state.tax_master_xlsx_limits),
            ).parse()
        except IncomeTaxRefundWorkbookError as error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                detail={
                    "code": "INVALID_REFUND_WORKBOOK",
                    "errors": [
                        {
                            "row_number": item.row_number,
                            "error_code": item.error_code,
                            "message": item.message,
                            "field": item.field,
                        }
                        for item in error.errors
                    ],
                },
            ) from error
        resolved_version = source_version or f"sha256:{sha256(payload).hexdigest()}"
        try:
            summary = _service(request).import_targets(
                refund_tax_year=refund_tax_year,
                source_version=resolved_version,
                drafts=tuple(
                    IncomeTaxRefundTargetDraft(
                        company_code=row.company_code,
                        source_record_key=row.source_record_key,
                        expected_refund_amount=row.expected_refund_amount,
                        raw_expected_refund_amount=row.raw_expected_refund_amount,
                        currency=currency,
                        amount_scale=amount_scale,
                        received_in_source=row.received_in_source,
                    )
                    for row in rows
                ),
            )
        except IncomeTaxRefundServiceError as error:
            _raise_service_error(error)
        request.state.audit_row_count = len(rows)
        return IncomeTaxRefundImportResponse(
            source_version=summary.source_version,
            accepted_count=summary.accepted_count,
            replayed_count=summary.replayed_count,
        )
    finally:
        semaphore.release()


@router.post(
    "/sap-evidence",
    response_model=IncomeTaxRefundSapEvidenceResponse,
    status_code=status.HTTP_201_CREATED,
)
def import_sap_evidence(
    body: IncomeTaxRefundSapEvidenceImportRequest,
    request: Request,
    _principal: Annotated[Principal, Depends(require_group_tax)],
) -> IncomeTaxRefundSapEvidenceResponse:
    draft = SapRefundEvidenceDraft(
        source_batch_key=body.source_batch_key,
        fiscal_year=body.fiscal_year,
        through_period=body.through_period,
        company_codes=body.company_codes,
        lines=tuple(
            SapRefundLineDraft(
                company_code=item.company_code,
                client=item.client,
                ledger=item.ledger,
                fiscal_year=item.fiscal_year,
                fiscal_period=item.fiscal_period,
                posting_date=item.posting_date,
                document_number=item.document_number,
                line_item=item.line_item,
                gl_account_code=item.gl_account_code,
                gl_account_name=item.gl_account_name,
                account_category=item.account_category,
                debit_credit=item.debit_credit,
                amount=item.amount,
                currency=item.currency,
                amount_scale=item.amount_scale,
                is_reversed=item.is_reversed,
            )
            for item in body.items
        ),
    )
    try:
        summary = _service(request).import_sap_evidence(draft)
    except IncomeTaxRefundServiceError as error:
        _raise_service_error(error)
    request.state.audit_row_count = summary.accepted_count + summary.replayed_count
    return IncomeTaxRefundSapEvidenceResponse(
        source_batch_key=summary.source_batch_key,
        accepted_count=summary.accepted_count,
        replayed_count=summary.replayed_count,
        complete_company_count=summary.complete_company_count,
    )


@router.post("/scans", response_model=IncomeTaxRefundScanResponse)
def run_scan(
    body: IncomeTaxRefundScanRequest,
    request: Request,
    principal: Annotated[Principal, Depends(require_group_tax)],
) -> IncomeTaxRefundScanResponse:
    try:
        view = _service(request).scan(
            refund_tax_year=body.refund_tax_year,
            scan_year=body.scan_year,
            scan_month=body.scan_month,
            source_batch_key=body.source_batch_key,
            allowed_company_ids=company_scope(principal),
        )
    except IncomeTaxRefundServiceError as error:
        _raise_service_error(error)
    dispatcher = cast(
        Callable[[], object],
        request.app.state.income_tax_refund_writeback_dispatcher,
    )
    dispatcher()
    request.state.audit_row_count = (
        view.received_count + view.not_received_count + view.ambiguous_count
    )
    return _response(view)


@router.get("/results", response_model=IncomeTaxRefundScanResponse)
def list_results(
    request: Request,
    principal: Annotated[Principal, Depends(require_reader)],
    refund_tax_year: Annotated[int, Query(ge=2000, le=9998)],
    scan_year: Annotated[int, Query(ge=2001, le=9999)],
    scan_month: Annotated[int, Query(ge=3, le=12)],
) -> IncomeTaxRefundScanResponse:
    try:
        view = _service(request).list_results(
            refund_tax_year=refund_tax_year,
            scan_year=scan_year,
            scan_month=scan_month,
            allowed_company_ids=company_scope(principal),
        )
    except IncomeTaxRefundServiceError as error:
        _raise_service_error(error)
    request.state.audit_row_count = (
        view.received_count + view.not_received_count + view.ambiguous_count
    )
    return _response(view)


def _response(view: IncomeTaxRefundSummaryView) -> IncomeTaxRefundScanResponse:
    return IncomeTaxRefundScanResponse(
        refund_tax_year=view.refund_tax_year,
        scan_period=view.scan_period,
        received_count=view.received_count,
        not_received_count=view.not_received_count,
        wrong_account_count=view.wrong_account_count,
        ambiguous_count=view.ambiguous_count,
        received=tuple(
            IncomeTaxRefundScanItemResponse.model_validate(item, from_attributes=True)
            for item in view.received
        ),
        not_received=tuple(
            IncomeTaxRefundScanItemResponse.model_validate(item, from_attributes=True)
            for item in view.not_received
        ),
        ambiguous=tuple(
            IncomeTaxRefundScanItemResponse.model_validate(item, from_attributes=True)
            for item in view.ambiguous
        ),
    )


def _raise_service_error(error: IncomeTaxRefundServiceError) -> Never:
    if error.error_code in {
        "REFUND_COMPANY_NOT_FOUND",
        "SAP_EVIDENCE_BATCH_NOT_FOUND",
    }:
        status_code = status.HTTP_404_NOT_FOUND
    elif error.error_code in {
        "REFUND_TARGET_IMMUTABLE",
        "SAP_EVIDENCE_BATCH_CONFLICT",
        "SAP_LINE_CONTENT_CONFLICT",
        "REFUND_SCAN_CONFLICT",
        "SAP_EVIDENCE_INCOMPLETE",
        "SAP_EVIDENCE_COMPANY_INCOMPLETE",
    }:
        status_code = status.HTTP_409_CONFLICT
    else:
        status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    raise HTTPException(
        status_code=status_code,
        detail={"code": error.error_code, "message": str(error)},
    )


class _FileTooLargeError(ValueError):
    pass


def _read_upload_limited(stream: BinaryIO, maximum_bytes: int) -> bytes:
    payload = bytearray()
    while chunk := stream.read(64 * 1024):
        if len(payload) + len(chunk) > maximum_bytes:
            raise _FileTooLargeError(f"uploaded file exceeds {maximum_bytes} bytes")
        payload.extend(chunk)
    return bytes(payload)


__all__ = ["router"]
