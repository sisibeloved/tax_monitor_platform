"""Thin governance API for semantic model, prompt, and case-library artifacts."""

from __future__ import annotations

from typing import Annotated, Never, cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request, status

from tax_risk.api.dependencies import require_group_tax
from tax_risk.api.schemas import (
    SemanticArtifactActionRequest,
    SemanticArtifactCreateRequest,
    SemanticArtifactResponse,
)
from tax_risk.application.semantic.version_registry import (
    SemanticArtifactConflictError,
    SemanticArtifactError,
    SemanticArtifactNotReadyError,
    SemanticVersionRegistry,
)
from tax_risk.persistence.repositories import UnitOfWork
from tax_risk.security.principal import Principal


router = APIRouter(
    prefix="/api/v1/semantic-artifacts",
    tags=["semantic-governance"],
    dependencies=[Depends(require_group_tax)],
)


def get_registry(request: Request) -> SemanticVersionRegistry:
    return SemanticVersionRegistry(cast(type[UnitOfWork], request.app.state.uow_factory))


@router.post("", response_model=SemanticArtifactResponse, status_code=status.HTTP_201_CREATED)
def create_artifact(
    body: SemanticArtifactCreateRequest,
    principal: Annotated[Principal, Depends(require_group_tax)],
    registry: SemanticVersionRegistry = Depends(get_registry),
) -> object:
    try:
        return registry.create(
            artifact_type=body.artifact_type,
            version=body.version,
            checksum=body.checksum,
            storage_ref=body.storage_ref,
            deployment_id=body.deployment_id,
            effective_from=body.effective_from,
            effective_to=body.effective_to,
            uploaded_by=principal.subject,
        )
    except SemanticArtifactError as error:
        _raise_http(error)


@router.post("/{artifact_id}/approve", response_model=SemanticArtifactResponse)
def approve_artifact(
    artifact_id: UUID,
    _body: SemanticArtifactActionRequest,
    principal: Annotated[Principal, Depends(require_group_tax)],
    registry: SemanticVersionRegistry = Depends(get_registry),
) -> object:
    try:
        return registry.approve(artifact_id, reviewed_by=principal.subject)
    except SemanticArtifactError as error:
        _raise_http(error)


@router.post("/{artifact_id}/publish", response_model=SemanticArtifactResponse)
def publish_artifact(
    artifact_id: UUID,
    _body: SemanticArtifactActionRequest,
    principal: Annotated[Principal, Depends(require_group_tax)],
    registry: SemanticVersionRegistry = Depends(get_registry),
) -> object:
    try:
        return registry.publish(artifact_id, published_by=principal.subject)
    except SemanticArtifactError as error:
        _raise_http(error)


def _raise_http(error: SemanticArtifactError) -> Never:
    if isinstance(error, SemanticArtifactNotReadyError):
        status_code = status.HTTP_404_NOT_FOUND
    elif isinstance(error, SemanticArtifactConflictError):
        status_code = status.HTTP_409_CONFLICT
    else:
        status_code = status.HTTP_422_UNPROCESSABLE_CONTENT
    raise HTTPException(
        status_code=status_code,
        detail={"code": error.error_code, "message": str(error)},
    )


__all__ = ["router"]
