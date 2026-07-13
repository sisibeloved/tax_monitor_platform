"""Monthly business-entertainment orchestration with fail-closed gates."""

from __future__ import annotations

from calendar import monthrange
from datetime import date, datetime
from hashlib import sha256
from typing import Protocol
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from tax_risk.domain.semantic.contracts import SemanticDetection


class BusinessEntertainmentRunError(Exception):
    def __init__(self, error_code: str, message: str, *, retryable: bool = False) -> None:
        self.error_code = error_code
        self.retryable = retryable
        super().__init__(message)


class BusinessEntertainmentRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    run_id: UUID
    company_code: str = Field(min_length=1, max_length=64)
    period_end: date
    snapshot_set_id: UUID
    rule_version_id: str = Field(min_length=1, max_length=128)
    lexicon_version: str = Field(min_length=1, max_length=128)
    model_version_id: str = Field(min_length=1, max_length=128)
    prompt_version_id: str = Field(min_length=1, max_length=128)
    case_library_version_id: str = Field(min_length=1, max_length=128)
    account_dictionary_version_id: str = Field(min_length=1, max_length=128)

    @field_validator("period_end")
    @classmethod
    def period_must_end_in_current_month(cls, value: date) -> date:
        if value.day != monthrange(value.year, value.month)[1]:
            raise ValueError("period_end must be the final day of a month")
        return value

    @property
    def idempotency_key(self) -> str:
        components = (
            str(self.run_id),
            self.company_code,
            self.period_end.isoformat(),
            str(self.snapshot_set_id),
            self.rule_version_id,
            self.lexicon_version,
            self.model_version_id,
            self.prompt_version_id,
            self.case_library_version_id,
            self.account_dictionary_version_id,
        )
        return sha256("\0".join(components).encode()).hexdigest()

    def to_task_kwargs(self) -> dict[str, str]:
        return {
            "run_id": str(self.run_id),
            "company_code": self.company_code,
            "period_end": self.period_end.isoformat(),
            "snapshot_set_id": str(self.snapshot_set_id),
            "rule_version_id": self.rule_version_id,
            "lexicon_version": self.lexicon_version,
            "model_version_id": self.model_version_id,
            "prompt_version_id": self.prompt_version_id,
            "case_library_version_id": self.case_library_version_id,
            "account_dictionary_version_id": self.account_dictionary_version_id,
        }


class PublishedCompanyInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    snapshot_status: str
    published_at: datetime | None
    source_record_count: int = Field(ge=0)
    exact_link_count: int = Field(ge=0)
    fuzzy_hint_count: int = Field(ge=0)
    conflict_count: int = Field(ge=0)
    sap_linked_candidate_keys: tuple[str, ...]
    business_unlinked_candidate_keys: tuple[str, ...]
    standalone_sap_keys: tuple[str, ...]


class BusinessEntertainmentPipeline(Protocol):
    def resolve_scope(self, period_end: date) -> tuple[str, ...]: ...

    def load_company_input(
        self,
        request: BusinessEntertainmentRunRequest,
    ) -> PublishedCompanyInput: ...

    def link_company_input(
        self,
        request: BusinessEntertainmentRunRequest,
        inputs: PublishedCompanyInput,
        *,
        idempotency_key: str,
    ) -> PublishedCompanyInput: ...

    def persist_coverages(
        self,
        request: BusinessEntertainmentRunRequest,
        *,
        linked_candidate_keys: tuple[str, ...],
        standalone_sap_keys: tuple[str, ...],
        idempotency_key: str,
    ) -> int: ...

    def generate_candidates(
        self,
        request: BusinessEntertainmentRunRequest,
        *,
        evaluation_candidate_keys: tuple[str, ...],
        idempotency_key: str,
    ) -> tuple[str, ...]: ...

    def evaluate_candidates(
        self,
        request: BusinessEntertainmentRunRequest,
        *,
        candidate_keys: tuple[str, ...],
        idempotency_key: str,
    ) -> tuple[SemanticDetection, ...]: ...

    def route_detections(
        self,
        request: BusinessEntertainmentRunRequest,
        *,
        detections: tuple[SemanticDetection, ...],
        idempotency_key: str,
    ) -> tuple[int, int, int]: ...


class BusinessEntertainmentMonthlyService:
    """Enforce immutable inputs and ordering around replaceable pipeline stages."""

    def __init__(self, pipeline: BusinessEntertainmentPipeline) -> None:
        self._pipeline = pipeline

    def run_company(
        self,
        request: BusinessEntertainmentRunRequest,
        *,
        task_id: str,
    ) -> dict[str, object]:
        scope = self._pipeline.resolve_scope(request.period_end)
        if request.company_code not in scope:
            raise BusinessEntertainmentRunError(
                "COMPANY_OUT_OF_SCOPE",
                f"company {request.company_code} is not in the published scope",
            )

        inputs = self._pipeline.load_company_input(request)
        if inputs.snapshot_status != "PUBLISHED":
            raise BusinessEntertainmentRunError(
                "SNAPSHOT_SET_NOT_PUBLISHED",
                "business-entertainment monitoring requires a PUBLISHED snapshot set",
            )
        if inputs.published_at is None:
            raise BusinessEntertainmentRunError(
                "SNAPSHOT_SET_PUBLICATION_TIME_MISSING",
                "published snapshot set must have an immutable publication time",
            )
        if inputs.published_at.tzinfo is None or inputs.published_at.utcoffset() is None:
            raise BusinessEntertainmentRunError(
                "SNAPSHOT_SET_PUBLICATION_TIME_INVALID",
                "snapshot publication time must be timezone-aware",
            )

        key = request.idempotency_key
        inputs = self._pipeline.link_company_input(
            request,
            inputs,
            idempotency_key=key,
        )
        coverage_count = self._pipeline.persist_coverages(
            request,
            linked_candidate_keys=inputs.sap_linked_candidate_keys,
            standalone_sap_keys=inputs.standalone_sap_keys,
            idempotency_key=key,
        )
        evaluation_keys = (
            inputs.sap_linked_candidate_keys + inputs.business_unlinked_candidate_keys
        )
        candidate_keys = self._pipeline.generate_candidates(
            request,
            evaluation_candidate_keys=evaluation_keys,
            idempotency_key=key,
        )
        detections = self._pipeline.evaluate_candidates(
            request,
            candidate_keys=candidate_keys,
            idempotency_key=key,
        )
        detection_count, evidence_task_count, risk_case_count = (
            self._pipeline.route_detections(
                request,
                detections=detections,
                idempotency_key=key,
            )
        )
        return {
            "run_id": str(request.run_id),
            "company_code": request.company_code,
            "status": "SUCCEEDED",
            "retryable": False,
            "task_id": task_id,
            "idempotency_key": key,
            "scope_company_count": len(scope),
            "source_record_count": inputs.source_record_count,
            "exact_link_count": inputs.exact_link_count,
            "fuzzy_hint_count": inputs.fuzzy_hint_count,
            "conflict_count": inputs.conflict_count,
            "sap_linked_evaluation_count": len(inputs.sap_linked_candidate_keys),
            "business_unlinked_evaluation_count": len(
                inputs.business_unlinked_candidate_keys
            ),
            "standalone_sap_count": len(inputs.standalone_sap_keys),
            "sap_coverage_count": coverage_count,
            "candidate_count": len(candidate_keys),
            "detection_count": detection_count,
            "evidence_task_count": evidence_task_count,
            "risk_case_count": risk_case_count,
        }


def build_default_business_entertainment_service() -> BusinessEntertainmentMonthlyService:
    import os

    from tax_risk.api.business_entertainment_dependencies import (
        BusinessEntertainmentDependencyError,
        bind_structured_model_client,
    )
    from tax_risk.application.business_entertainment.production_pipeline import (
        DatabaseBusinessEntertainmentPipeline,
    )
    from tax_risk.config import Settings
    from tax_risk.persistence.repositories import UnitOfWork

    settings = Settings()
    try:
        model_client = bind_structured_model_client(
            settings,
            credential_resolver=lambda reference: os.environ.get(reference, ""),
            uow_factory=UnitOfWork,
        )
    except BusinessEntertainmentDependencyError as error:
        raise BusinessEntertainmentRunError(
            "SEMANTIC_MODEL_CONFIGURATION_INVALID",
            str(error),
        ) from error
    return BusinessEntertainmentMonthlyService(
        DatabaseBusinessEntertainmentPipeline(
            uow_factory=UnitOfWork,
            model_client=model_client,
        )
    )


__all__ = [
    "BusinessEntertainmentMonthlyService",
    "BusinessEntertainmentPipeline",
    "BusinessEntertainmentRunError",
    "BusinessEntertainmentRunRequest",
    "PublishedCompanyInput",
    "build_default_business_entertainment_service",
]
