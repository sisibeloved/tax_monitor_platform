from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)

from tax_risk.persistence.ingest_models import IngestBatchStatus, IngestMode
from tax_risk.persistence.master_models import VersionStatus
from tax_risk.persistence.risk_models import (
    CalculationStatus,
    MonitorType,
    MonitoringRunCompanyStatus,
    MonitoringRunStatus,
    RiskCaseStatus,
)
from tax_risk.persistence.snapshot_models import SnapshotSetStatus, SnapshotStatus
from tax_risk.snapshot_limits import (
    MAX_SNAPSHOT_SET_MEMBERS,
    MAX_SNAPSHOT_SOURCE_BATCHES,
)
from tax_risk.application.semantic.version_registry import (
    ArtifactStatus,
    ArtifactType,
)


class IngestBatchCreate(BaseModel):
    source: str = Field(min_length=1, max_length=64)
    source_batch_key: str = Field(min_length=1, max_length=256)
    dataset_code: str = Field(min_length=1, max_length=128)
    extraction_time: datetime
    period: date
    mode: IngestMode
    schema_version: str = Field(min_length=1, max_length=64)
    currency: str = Field(min_length=3, max_length=3, pattern=r"^[A-Z]{3}$")
    amount_scale: int = Field(ge=0, le=12)
    source_primary_key_definition: dict[str, Any] = Field(default_factory=dict)

    @field_validator("source", "source_batch_key", "dataset_code", "schema_version")
    @classmethod
    def strip_nonempty_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must not be blank")
        return stripped

    @field_validator("extraction_time")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("extraction_time must include a UTC offset")
        return value


class IngestErrorResponse(BaseModel):
    row_number: int
    error_code: str
    message: str
    details: dict[str, Any]
    retryable: bool

    model_config = ConfigDict(from_attributes=True)


class SemanticArtifactCreateRequest(BaseModel):
    artifact_type: ArtifactType
    version: str = Field(min_length=1, max_length=128)
    checksum: str = Field(min_length=64, max_length=64)
    storage_ref: str = Field(min_length=1, max_length=512)
    deployment_id: str | None = Field(default=None, max_length=256)
    effective_from: date
    effective_to: date


class SemanticArtifactActionRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class SemanticArtifactResponse(BaseModel):
    artifact_id: UUID
    artifact_type: ArtifactType
    version: str
    checksum: str
    storage_ref: str
    deployment_id: str | None
    effective_from: date
    effective_to: date
    status: ArtifactStatus
    uploaded_by: str
    reviewer_id: str | None
    published_by: str | None
    approved_at: datetime | None
    published_at: datetime | None

    model_config = ConfigDict(from_attributes=True)


class IngestBatchResponse(BaseModel):
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
    errors: tuple[IngestErrorResponse, ...]
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TaxMasterImportResponse(BaseModel):
    batch_id: UUID
    checksum: str
    source_filename: str
    uploaded_by: str
    currency: str
    amount_scale: int
    version_ids: tuple[UUID, ...]
    imported_at: datetime
    replayed: bool

    model_config = ConfigDict(from_attributes=True)


class TaxMasterApproveRequest(BaseModel):
    reviewed_by: str = Field(min_length=1, max_length=256)

    @field_validator("reviewed_by")
    @classmethod
    def require_nonblank_reviewer(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("reviewed_by must not be blank")
        return value


class TaxMasterResponse(BaseModel):
    id: UUID
    source_batch_id: UUID
    company_code: str
    company_name: str
    valid_from: date
    valid_to: date | None
    version: str
    status: VersionStatus
    tax_rate: Decimal
    loss_carryforward: Decimal
    three_year_average_tax_burden: Decimal
    currency: str
    amount_scale: int
    source_filename: str | None
    source_checksum: str | None
    source_row_number: int
    uploaded_by: str
    imported_at: datetime
    published_at: datetime | None
    approved_by: str | None

    model_config = ConfigDict(from_attributes=True)


class SnapshotValidateRequest(BaseModel):
    company_code: str
    period: date
    source_batch_ids: tuple[UUID, ...] = Field(
        max_length=MAX_SNAPSHOT_SOURCE_BATCHES
    )
    accepted_partial_batch_ids: tuple[UUID, ...] = Field(
        default=(),
        max_length=MAX_SNAPSHOT_SOURCE_BATCHES,
    )


class SnapshotQualityIssueResponse(BaseModel):
    category: str
    error_code: str
    source: str
    field: str
    company: str
    period: date
    remediation: str

    model_config = ConfigDict(from_attributes=True)


class SnapshotResponse(BaseModel):
    id: UUID
    company_id: UUID
    company_code: str
    tax_master_version_id: UUID
    period: date
    source_version_set_hash: str
    status: SnapshotStatus
    currency: str
    amount_scale: int
    record_count: int
    control_total: Decimal
    checksum: str
    lineage: dict[str, Any]
    published_at: datetime | None

    model_config = ConfigDict(from_attributes=True)


class SnapshotValidationResponse(BaseModel):
    valid: bool
    issues: tuple[SnapshotQualityIssueResponse, ...]
    snapshot: SnapshotResponse | None
    reused: bool

    model_config = ConfigDict(from_attributes=True)


class SnapshotSetMemberRequest(BaseModel):
    company_id: UUID
    snapshot_id: UUID


class SnapshotSetCreateRequest(BaseModel):
    set_key: str
    period: date
    expected_members: tuple[SnapshotSetMemberRequest, ...] = Field(
        max_length=MAX_SNAPSHOT_SET_MEMBERS
    )
    supersedes_snapshot_set_id: UUID | None = None


class SnapshotSetResponse(BaseModel):
    id: UUID
    set_key: str
    period: date
    status: SnapshotSetStatus
    expected_member_count: int
    published_at: datetime
    supersedes_snapshot_set_id: UUID | None
    members: tuple[SnapshotSetMemberRequest, ...]

    model_config = ConfigDict(from_attributes=True)


class QuarterlyRunCreateRequest(BaseModel):
    fiscal_year: int = Field(ge=2000, le=9999)
    quarter: int = Field(ge=1, le=4)
    snapshot_set_id: UUID
    rule_version: UUID


class QuarterlyRunStartResponse(BaseModel):
    run_id: UUID
    run_key: str
    status: MonitoringRunStatus
    dispatched_company_count: int


class QuarterlyRunResponse(BaseModel):
    id: UUID
    run_key: str
    status: MonitoringRunStatus
    fiscal_year: int
    quarter: int
    snapshot_set_id: UUID
    rule_version_id: UUID
    requested_company_count: int
    succeeded_company_count: int
    blocked_company_count: int
    failed_company_count: int
    started_at: datetime | None
    finished_at: datetime | None
    failure_reason: str | None

    model_config = ConfigDict(from_attributes=True)


class RiskCaseItemResponse(BaseModel):
    id: UUID
    company_id: UUID
    company_code: str
    company_name: str
    latest_detection_id: UUID | None
    run_id: UUID | None = None
    monitoring_type: MonitorType
    calculation_status: CalculationStatus | None = None
    input_amount: Decimal | None
    result_amount: Decimal | None
    difference_amount: Decimal | None
    tax_burden_rate: Decimal | None
    tax_burden_deviation: Decimal | None
    not_calculated_reason: str | None
    alert_code: str | None
    risk_direction: str
    risk_amount: Decimal | None
    risk_rate: Decimal | None
    currency: str
    amount_scale: int
    status: RiskCaseStatus
    priority: int
    assignee: str | None
    row_version: int
    fiscal_year: int | None = None
    period: int | None = None
    source_mode: str | None = None
    sap_link_status: str | None = None
    sap_document_number: str | None = None
    sap_line_item: str | None = None
    semantic_label: str | None = None
    confidence_tier: str | None = None
    workflow_note: str | None = None

    @field_serializer(
        "input_amount",
        "result_amount",
        "difference_amount",
        "tax_burden_rate",
        "tax_burden_deviation",
        "risk_amount",
        "risk_rate",
    )
    def serialize_risk_decimal(self, value: Decimal | None) -> str | None:
        return None if value is None else format(value, "f")


class RiskCaseListResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: tuple[RiskCaseItemResponse, ...]


class ResolutionEvidenceLinkResponse(BaseModel):
    evidence_link_id: UUID
    relation_quality: str
    matched_field: str
    sap_document_number: str
    sap_line_item: str

    model_config = ConfigDict(from_attributes=True)


class BusinessEntertainmentCaseDetailResponse(BaseModel):
    case_id: UUID
    company_id: UUID
    company_code: str
    company_name: str
    status: RiskCaseStatus
    merged_into_case_id: UUID | None
    canonical_source_record_id: UUID
    source_mode: str
    sap_link_status: str
    sap_document_number: str | None
    sap_line_item: str | None
    risk_amount: Decimal
    currency: str
    risk_amount_source: str
    semantic_label: str
    confidence_tier: str
    evidence_refs: tuple[dict[str, str], ...]
    recommended_account_ids: tuple[str, ...]
    rationale_summary: str
    missing_evidence: tuple[str, ...]
    rule_version_id: str
    model_version_id: str
    prompt_version_id: str
    case_library_version_id: str
    account_dictionary_version: str
    workflow_note: str
    row_version: int
    resolution_evidence_links: tuple[ResolutionEvidenceLinkResponse, ...]

    model_config = ConfigDict(from_attributes=True)

    @field_serializer("risk_amount")
    def serialize_risk_amount(self, value: Decimal) -> str:
        return format(value, "f")


class SapLinkCoverageItemResponse(BaseModel):
    coverage_id: UUID
    company_id: UUID
    company_code: str
    company_name: str
    period: date
    document_number: str
    line_item: str
    amount: Decimal
    currency: str
    link_status: str
    exact_evidence_link_id: UUID | None
    evaluated_via_business_document: bool
    snapshot_id: UUID

    model_config = ConfigDict(from_attributes=True)

    @field_serializer("amount")
    def serialize_amount(self, value: Decimal) -> str:
        return format(value, "f")


class SapLinkCoverageListResponse(BaseModel):
    total: int
    items: tuple[SapLinkCoverageItemResponse, ...]


class ResolveBusinessEntertainmentCaseRequest(BaseModel):
    evidence_link_id: UUID
    expected_row_version: int = Field(ge=1)


class ResolveBusinessEntertainmentCaseResponse(BaseModel):
    source_case_id: UUID
    root_case_id: UUID
    evidence_link_id: UUID
    merged: bool

    model_config = ConfigDict(from_attributes=True)


class RiskCaseAction(StrEnum):
    ASSIGN = "ASSIGN"
    REQUEST_COMPANY_CONFIRMATION = "REQUEST_COMPANY_CONFIRMATION"
    REQUEST_ADJUSTMENT = "REQUEST_ADJUSTMENT"
    SUBMIT_ADJUSTMENT = "SUBMIT_ADJUSTMENT"
    SUBMIT_GROUP_REVIEW = "SUBMIT_GROUP_REVIEW"
    REQUEST_EVIDENCE = "REQUEST_EVIDENCE"
    RESUBMIT_CONFIRMATION = "RESUBMIT_CONFIRMATION"
    CLOSE = "CLOSE"


class RiskCaseActionRequest(BaseModel):
    action: RiskCaseAction
    to_status: RiskCaseStatus
    reason: str = Field(min_length=1)
    assignee: str | None = Field(default=None, max_length=256)
    attachment_refs: tuple[str, ...] = ()
    correction_voucher_no: str | None = Field(default=None, max_length=128)

    @field_validator("reason")
    @classmethod
    def strip_case_action_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("value must not be blank")
        return stripped

    @field_validator("assignee")
    @classmethod
    def strip_case_assignee(cls, value: str | None) -> str | None:
        return None if value is None else value.strip()

    @model_validator(mode="after")
    def validate_assignee_action_contract(self) -> RiskCaseActionRequest:
        if self.action == RiskCaseAction.ASSIGN:
            if not self.assignee:
                raise ValueError("ASSIGN requires a non-empty assignee")
        elif self.assignee is not None:
            raise ValueError("assignee is allowed only for ASSIGN")
        return self


class RiskCaseActionResponse(BaseModel):
    id: UUID
    status: RiskCaseStatus
    assignee: str | None
    row_version: int

    model_config = ConfigDict(from_attributes=True)


class DashboardCompanyResponse(BaseModel):
    company_id: UUID
    company_code: str
    company_name: str
    data_ready: bool
    execution_status: MonitoringRunCompanyStatus
    blocked_reason: str | None
    risk_count: int


class DashboardCompanyPageResponse(BaseModel):
    total: int
    page: int
    page_size: int
    items: tuple[DashboardCompanyResponse, ...]


class QuarterlyDashboardResponse(BaseModel):
    fiscal_year: int
    quarter: int
    run_id: UUID
    coverage_company_count: int
    data_ready_count: int
    blocked_count: int
    risk_company_count: int
    potential_tax_cost_total: Decimal
    currency: str
    amount_scale: int
    monitoring_type_counts: dict[MonitorType, int]
    companies: DashboardCompanyPageResponse

    @field_serializer("potential_tax_cost_total")
    def serialize_potential_tax_cost(self, value: Decimal) -> str:
        return format(value, "f")


class DetectionDetailResponse(BaseModel):
    id: UUID
    run_id: UUID
    company_id: UUID
    snapshot_id: UUID
    rule_version_id: UUID
    tax_master_version_id: UUID
    monitoring_type: MonitorType
    calculation_status: CalculationStatus
    input_amount: Decimal | None
    result_amount: Decimal | None
    difference_amount: Decimal | None
    rate_value: Decimal | None
    tax_burden_rate: Decimal | None
    tax_burden_deviation: Decimal | None
    currency: str
    amount_scale: int
    formula_substitution: dict[str, Any]
    lineage: dict[str, Any]
    structured_output: dict[str, Any]
    not_calculated_reason: str | None
    alert_code: str | None
    direction: str | None

    @field_serializer(
        "input_amount",
        "result_amount",
        "difference_amount",
        "rate_value",
        "tax_burden_rate",
        "tax_burden_deviation",
    )
    def serialize_detection_decimal(self, value: Decimal | None) -> str | None:
        return None if value is None else format(value, "f")


__all__ = [
    "IngestBatchCreate",
    "IngestBatchResponse",
    "IngestErrorResponse",
    "DashboardCompanyPageResponse",
    "DashboardCompanyResponse",
    "DetectionDetailResponse",
    "QuarterlyDashboardResponse",
    "QuarterlyRunCreateRequest",
    "QuarterlyRunResponse",
    "QuarterlyRunStartResponse",
    "RiskCaseAction",
    "RiskCaseActionRequest",
    "RiskCaseActionResponse",
    "RiskCaseItemResponse",
    "RiskCaseListResponse",
    "TaxMasterApproveRequest",
    "TaxMasterImportResponse",
    "TaxMasterResponse",
    "SnapshotQualityIssueResponse",
    "SnapshotResponse",
    "SnapshotSetCreateRequest",
    "SnapshotSetMemberRequest",
    "SnapshotSetResponse",
    "SnapshotValidateRequest",
    "SnapshotValidationResponse",
]
