"""Maker-checker registry for model, prompt, and case-library artifacts."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from datetime import date, datetime, timezone
from enum import StrEnum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from tax_risk.domain.semantic.contracts import SemanticVersionSet
from tax_risk.persistence.repositories import UnitOfWork
from tax_risk.persistence.semantic_models import SemanticArtifactVersion


class ArtifactType(StrEnum):
    MODEL = "MODEL"
    PROMPT = "PROMPT"
    CASE_LIBRARY = "CASE_LIBRARY"


class ArtifactStatus(StrEnum):
    DRAFT = "DRAFT"
    APPROVED = "APPROVED"
    PUBLISHED = "PUBLISHED"
    RETIRED = "RETIRED"


class SemanticArtifact(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    artifact_id: UUID
    artifact_type: ArtifactType
    version: str = Field(min_length=1, max_length=128)
    checksum: str = Field(min_length=64, max_length=64)
    storage_ref: str = Field(min_length=1, max_length=512)
    deployment_id: str | None = Field(default=None, max_length=256)
    effective_from: date
    effective_to: date
    status: ArtifactStatus
    uploaded_by: str
    reviewer_id: str | None
    published_by: str | None
    approved_at: datetime | None
    published_at: datetime | None

    @field_validator("checksum")
    @classmethod
    def checksum_is_hex(cls, value: str) -> str:
        if any(character not in "0123456789abcdef" for character in value.lower()):
            raise ValueError("checksum must be hexadecimal")
        return value.lower()

    @model_validator(mode="after")
    def model_has_deployment_and_period_is_valid(self) -> SemanticArtifact:
        if self.effective_to < self.effective_from:
            raise ValueError("effective_to must not precede effective_from")
        if self.artifact_type is ArtifactType.MODEL and not self.deployment_id:
            raise ValueError("model artifact requires deployment_id")
        return self


class SemanticArtifactError(Exception):
    error_code = "SEMANTIC_ARTIFACT_ERROR"


class SemanticArtifactConflictError(SemanticArtifactError):
    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        super().__init__(message)


class SemanticArtifactNotReadyError(SemanticArtifactError):
    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        super().__init__(message)


class SemanticVersionRegistry:
    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    def create(
        self,
        *,
        artifact_type: ArtifactType,
        version: str,
        checksum: str,
        storage_ref: str,
        deployment_id: str | None,
        effective_from: date,
        effective_to: date,
        uploaded_by: str,
    ) -> SemanticArtifact:
        candidate = SemanticArtifact(
            artifact_id=UUID(int=0),
            artifact_type=artifact_type,
            version=version,
            checksum=checksum,
            storage_ref=storage_ref,
            deployment_id=deployment_id,
            effective_from=effective_from,
            effective_to=effective_to,
            status=ArtifactStatus.DRAFT,
            uploaded_by=uploaded_by,
            reviewer_id=None,
            published_by=None,
            approved_at=None,
            published_at=None,
        )
        with self._uow_factory() as uow:
            model = SemanticArtifactVersion(
                artifact_type=candidate.artifact_type.value,
                version=candidate.version,
                checksum=candidate.checksum,
                storage_ref=candidate.storage_ref,
                deployment_id=candidate.deployment_id,
                effective_from=candidate.effective_from,
                effective_to=candidate.effective_to,
                status=ArtifactStatus.DRAFT.value,
                uploaded_by=candidate.uploaded_by,
                reviewer_id=None,
                published_by=None,
                approved_at=None,
                published_at=None,
            )
            uow.semantic.add_semantic_artifact(model)
            uow.session.flush()
            result = _view(model)
            uow.commit()
            return result

    def approve(self, artifact_id: UUID, *, reviewed_by: str) -> SemanticArtifact:
        with self._uow_factory() as uow:
            artifact = self._load(uow, artifact_id, for_update=True)
            if artifact.status != ArtifactStatus.DRAFT.value:
                raise SemanticArtifactConflictError(
                    "SEMANTIC_ARTIFACT_STATE_CONFLICT",
                    f"artifact cannot be approved from {artifact.status}",
                )
            if artifact.uploaded_by.strip().casefold() == reviewed_by.strip().casefold():
                raise SemanticArtifactConflictError(
                    "MAKER_REVIEWER_CONFLICT",
                    "reviewer must be different from the uploader",
                )
            artifact.status = ArtifactStatus.APPROVED.value
            artifact.reviewer_id = reviewed_by.strip()
            artifact.approved_at = datetime.now(timezone.utc)
            uow.session.flush()
            result = _view(artifact)
            uow.commit()
            return result

    def publish(self, artifact_id: UUID, *, published_by: str) -> SemanticArtifact:
        with self._uow_factory() as uow:
            artifact = self._load(uow, artifact_id, for_update=True)
            if artifact.status != ArtifactStatus.APPROVED.value:
                raise SemanticArtifactConflictError(
                    "SEMANTIC_ARTIFACT_STATE_CONFLICT",
                    f"artifact cannot be published from {artifact.status}",
                )
            if uow.semantic.overlapping_published_semantic_artifacts(artifact):
                raise SemanticArtifactConflictError(
                    "SEMANTIC_ARTIFACT_PERIOD_OVERLAP",
                    "published artifact effective periods must not overlap by type",
                )
            artifact.status = ArtifactStatus.PUBLISHED.value
            artifact.published_by = published_by.strip()
            artifact.published_at = datetime.now(timezone.utc)
            uow.session.flush()
            result = _view(artifact)
            uow.commit()
            return result

    def resolve_active(
        self,
        *,
        effective_on: date,
        rule_version_id: str,
        account_dictionary_version: str,
    ) -> SemanticVersionSet:
        with self._uow_factory() as uow:
            artifacts = tuple(
                (artifact.artifact_type, artifact.version)
                for artifact in uow.semantic.active_semantic_artifacts(effective_on)
            )
        counts = Counter(artifact_type for artifact_type, _ in artifacts)
        required = {artifact_type.value for artifact_type in ArtifactType}
        if set(counts) != required or any(count != 1 for count in counts.values()):
            raise SemanticArtifactNotReadyError(
                "SEMANTIC_ARTIFACTS_NOT_PUBLISHED",
                "exactly one published artifact of each type must be effective",
            )
        by_type = dict(artifacts)
        return SemanticVersionSet(
            rule_version_id=rule_version_id,
            model_version_id=by_type[ArtifactType.MODEL.value],
            prompt_version_id=by_type[ArtifactType.PROMPT.value],
            case_library_version_id=by_type[ArtifactType.CASE_LIBRARY.value],
            account_dictionary_version=account_dictionary_version,
        )

    @staticmethod
    def _load(
        uow: UnitOfWork,
        artifact_id: UUID,
        *,
        for_update: bool,
    ) -> SemanticArtifactVersion:
        artifact = uow.semantic.get_semantic_artifact(artifact_id, for_update=for_update)
        if artifact is None:
            raise SemanticArtifactNotReadyError(
                "SEMANTIC_ARTIFACT_NOT_FOUND", "semantic artifact was not found"
            )
        return artifact


def _view(model: SemanticArtifactVersion) -> SemanticArtifact:
    return SemanticArtifact(
        artifact_id=model.id,
        artifact_type=ArtifactType(model.artifact_type),
        version=model.version,
        checksum=model.checksum,
        storage_ref=model.storage_ref,
        deployment_id=model.deployment_id,
        effective_from=model.effective_from,
        effective_to=model.effective_to,
        status=ArtifactStatus(model.status),
        uploaded_by=model.uploaded_by,
        reviewer_id=model.reviewer_id,
        published_by=model.published_by,
        approved_at=model.approved_at,
        published_at=model.published_at,
    )


__all__ = [
    "ArtifactStatus",
    "ArtifactType",
    "SemanticArtifact",
    "SemanticArtifactConflictError",
    "SemanticArtifactNotReadyError",
    "SemanticVersionRegistry",
]
