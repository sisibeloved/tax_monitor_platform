from __future__ import annotations

import calendar
from datetime import date
import logging
import re
from threading import BoundedSemaphore
from typing import Never, cast
from uuid import UUID

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
    status,
)

from tax_risk.adapters.ingest.tax_master_xlsx import XlsxResourceLimits
from tax_risk.api.routes.ingest import FileTooLargeError, _read_upload_limited
from tax_risk.api.schemas import (
    TaxMasterApproveRequest,
    TaxMasterImportResponse,
    TaxMasterResponse,
)
from tax_risk.application.master_data import (
    MasterDataConflictError,
    MasterDataError,
    MasterDataNotFoundError,
    MasterDataValidationError,
    TaxMasterService,
    UowFactory,
)


router = APIRouter(prefix="/api/v1/tax-master", tags=["tax-master"])
logger = logging.getLogger(__name__)
_QUARTER = re.compile(r"(?P<year>[0-9]{4})-Q(?P<quarter>[1-4])")


def get_tax_master_service(request: Request) -> TaxMasterService:
    return TaxMasterService(
        cast(UowFactory, request.app.state.uow_factory),
        xlsx_limits=cast(XlsxResourceLimits, request.app.state.tax_master_xlsx_limits),
    )


@router.post(
    "/import",
    response_model=TaxMasterImportResponse,
    status_code=status.HTTP_201_CREATED,
)
def import_tax_master(
    request: Request,
    response: Response,
    file: UploadFile = File(...),
    uploaded_by: str = Form(...),
    currency: str = Form("CNY"),
    amount_scale: str = Form("2"),
    service: TaxMasterService = Depends(get_tax_master_service),
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
            parsed_scale = _parse_amount_scale(amount_scale)
            result = service.import_xlsx(
                filename=file.filename or "upload.xlsx",
                payload=payload,
                uploaded_by=uploaded_by,
                currency=currency,
                amount_scale=parsed_scale,
            )
        except MasterDataError as error:
            _raise_http(error)
        except HTTPException:
            raise
        except Exception as error:
            logger.exception("tax_master_import_failed")
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail={
                    "code": "TAX_MASTER_IMPORT_FAILED",
                    "message": "tax master import failed",
                },
            ) from error
        if result.replayed:
            response.status_code = status.HTTP_200_OK
        return result
    finally:
        semaphore.release()


@router.post("/{version_id}/approve", response_model=TaxMasterResponse)
def approve_tax_master(
    version_id: UUID,
    request: TaxMasterApproveRequest,
    service: TaxMasterService = Depends(get_tax_master_service),
) -> object:
    try:
        return service.approve(version_id, reviewed_by=request.reviewed_by)
    except MasterDataError as error:
        _raise_http(error)
    except Exception as error:
        logger.exception("tax_master_approval_failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "TAX_MASTER_APPROVAL_FAILED",
                "message": "tax master approval failed",
            },
        ) from error


@router.get("/{company_code}", response_model=TaxMasterResponse)
def lookup_tax_master(
    company_code: str,
    period: str = Query(...),
    service: TaxMasterService = Depends(get_tax_master_service),
) -> object:
    effective_on = _quarter_end(period)
    try:
        return service.lookup(company_code, effective_on=effective_on)
    except MasterDataError as error:
        _raise_http(error)
    except Exception as error:
        logger.exception("tax_master_lookup_failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "TAX_MASTER_LOOKUP_FAILED",
                "message": "tax master lookup failed",
            },
        ) from error


def _quarter_end(period: str) -> date:
    matched = _QUARTER.fullmatch(period)
    if matched is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "INVALID_QUARTER_PERIOD",
                "message": "period must use YYYY-QN with quarter 1 through 4",
            },
        )
    year = int(matched.group("year"))
    if year < 2000:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail={
                "code": "INVALID_QUARTER_PERIOD",
                "message": "period year must be between 2000 and 9999",
            },
        )
    month = int(matched.group("quarter")) * 3
    return date(year, month, calendar.monthrange(year, month)[1])


def _parse_amount_scale(value: str) -> int:
    if not re.fullmatch(r"[0-9]+", value):
        _raise_import_option("amount_scale must be an integer between 0 and 12")
    parsed = int(value)
    if not 0 <= parsed <= 12:
        _raise_import_option("amount_scale must be an integer between 0 and 12")
    return parsed


def _raise_import_option(message: str) -> Never:
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        detail={"code": "INVALID_IMPORT_OPTIONS", "message": message},
    )


def _raise_http(error: MasterDataError) -> Never:
    if isinstance(error, MasterDataNotFoundError):
        status_code = status.HTTP_404_NOT_FOUND
        code = error.error_code
    elif isinstance(error, MasterDataConflictError):
        status_code = status.HTTP_409_CONFLICT
        code = error.error_code
    elif isinstance(error, MasterDataValidationError):
        status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
        code = error.issues[0].error_code if error.issues else error.error_code
    else:
        status_code = status.HTTP_400_BAD_REQUEST
        code = error.error_code

    detail: dict[str, object] = {"code": code, "message": str(error)}
    if isinstance(error, MasterDataValidationError):
        if error.batch_id is not None:
            detail["batch_id"] = str(error.batch_id)
        detail["issues"] = [
            {
                "row_number": issue.row_number,
                "code": issue.error_code,
                "message": issue.message,
                "field": issue.field,
                "rejected_value": issue.rejected_value,
            }
            for issue in error.issues
        ]
    raise HTTPException(status_code=status_code, detail=detail)


__all__ = ["router"]
