from __future__ import annotations

import logging
from typing import Never, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status

from tax_risk.api.schemas import (
    SnapshotResponse,
    SnapshotSetCreateRequest,
    SnapshotSetResponse,
    SnapshotValidateRequest,
    SnapshotValidationResponse,
)
from tax_risk.application.snapshots import (
    ExpectedSnapshotMember,
    SnapshotConflictError,
    SnapshotError,
    SnapshotNotFoundError,
    SnapshotQualityError,
    SnapshotRequestError,
    SnapshotService,
    UowFactory,
)


router = APIRouter(tags=["snapshots"])
logger = logging.getLogger(__name__)


def get_snapshot_service(request: Request) -> SnapshotService:
    return SnapshotService(cast(UowFactory, request.app.state.uow_factory))


@router.post(
    "/api/v1/snapshots/validate",
    response_model=SnapshotValidationResponse,
)
def validate_snapshot(
    request: SnapshotValidateRequest,
    service: SnapshotService = Depends(get_snapshot_service),
) -> object:
    try:
        return service.validate(
            company_code=request.company_code,
            period=request.period,
            source_batch_ids=request.source_batch_ids,
            accepted_partial_batch_ids=request.accepted_partial_batch_ids,
        )
    except SnapshotError as error:
        _raise_http(error)
    except Exception as error:
        logger.exception("snapshot_validation_failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={"code": "SNAPSHOT_VALIDATION_FAILED", "message": "snapshot validation failed"},
        ) from error


@router.post(
    "/api/v1/snapshots/{snapshot_id}/publish",
    response_model=SnapshotResponse,
)
def publish_snapshot(
    snapshot_id: UUID,
    service: SnapshotService = Depends(get_snapshot_service),
) -> object:
    try:
        return service.publish(snapshot_id)
    except SnapshotError as error:
        _raise_http(error)
    except Exception as error:
        logger.exception("snapshot_publication_failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "SNAPSHOT_PUBLICATION_FAILED",
                "message": "snapshot publication failed",
            },
        ) from error


@router.post(
    "/api/v1/snapshot-sets",
    response_model=SnapshotSetResponse,
    status_code=status.HTTP_201_CREATED,
)
def publish_snapshot_set(
    request: SnapshotSetCreateRequest,
    service: SnapshotService = Depends(get_snapshot_service),
) -> object:
    try:
        return service.publish_set(
            set_key=request.set_key,
            period=request.period,
            expected_members=tuple(
                ExpectedSnapshotMember(
                    company_id=member.company_id,
                    snapshot_id=member.snapshot_id,
                )
                for member in request.expected_members
            ),
            supersedes_snapshot_set_id=request.supersedes_snapshot_set_id,
        )
    except SnapshotError as error:
        _raise_http(error)
    except Exception as error:
        logger.exception("snapshot_set_publication_failed")
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail={
                "code": "SNAPSHOT_SET_PUBLICATION_FAILED",
                "message": "snapshot set publication failed",
            },
        ) from error


def _raise_http(error: SnapshotError) -> Never:
    if isinstance(error, SnapshotNotFoundError):
        status_code = status.HTTP_404_NOT_FOUND
    elif isinstance(error, SnapshotConflictError):
        status_code = status.HTTP_409_CONFLICT
    elif isinstance(error, (SnapshotRequestError, SnapshotQualityError)):
        status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    else:
        status_code = status.HTTP_400_BAD_REQUEST
    detail: dict[str, object] = {
        "code": error.error_code,
        "message": str(error),
    }
    if isinstance(error, SnapshotQualityError):
        detail["issues"] = [
            {
                "category": issue.category,
                "error_code": issue.error_code,
                "source": issue.source,
                "field": issue.field,
                "company": issue.company,
                "period": issue.period.isoformat(),
                "remediation": issue.remediation,
            }
            for issue in error.issues
        ]
    raise HTTPException(status_code=status_code, detail=detail)


__all__ = ["router"]
