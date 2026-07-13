"""Risk-case identity and workflow invariants."""

from __future__ import annotations

from enum import StrEnum
from hashlib import sha256
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class CaseStatus(StrEnum):
    """Supported risk-case workflow states."""

    NEW = "NEW"
    ASSIGNED = "ASSIGNED"
    PENDING_COMPANY_CONFIRMATION = "PENDING_COMPANY_CONFIRMATION"
    PENDING_ADJUSTMENT = "PENDING_ADJUSTMENT"
    ADJUSTED_PENDING_REVIEW = "ADJUSTED_PENDING_REVIEW"
    GROUP_REVIEW = "GROUP_REVIEW"
    EVIDENCE_REQUIRED = "EVIDENCE_REQUIRED"
    CLOSED = "CLOSED"


class InvalidCaseTransition(ValueError):
    """Raised when a case workflow transition is not permitted."""


class MonitorType(StrEnum):
    """Supported deterministic and semantic income-tax monitors."""

    ACCRUAL_ACCURACY = "ACCRUAL_ACCURACY"
    TAX_BURDEN = "TAX_BURDEN"
    POTENTIAL_TAX_COST = "POTENTIAL_TAX_COST"
    BUSINESS_ENTERTAINMENT = "BUSINESS_ENTERTAINMENT"
    WELFARE = "WELFARE"
    DONATION = "DONATION"


_ALLOWED_TRANSITIONS: dict[CaseStatus, frozenset[CaseStatus]] = {
    CaseStatus.NEW: frozenset({CaseStatus.ASSIGNED}),
    CaseStatus.ASSIGNED: frozenset({CaseStatus.PENDING_COMPANY_CONFIRMATION}),
    CaseStatus.PENDING_COMPANY_CONFIRMATION: frozenset(
        {
            CaseStatus.PENDING_ADJUSTMENT,
            CaseStatus.GROUP_REVIEW,
            CaseStatus.EVIDENCE_REQUIRED,
        }
    ),
    CaseStatus.PENDING_ADJUSTMENT: frozenset({CaseStatus.ADJUSTED_PENDING_REVIEW}),
    CaseStatus.ADJUSTED_PENDING_REVIEW: frozenset({CaseStatus.CLOSED}),
    CaseStatus.GROUP_REVIEW: frozenset({CaseStatus.CLOSED}),
    CaseStatus.EVIDENCE_REQUIRED: frozenset({CaseStatus.PENDING_COMPANY_CONFIRMATION}),
    CaseStatus.CLOSED: frozenset(),
}


def case_fingerprint(
    company_code: str,
    fiscal_year: int,
    quarter: int,
    monitor_type: str,
) -> str:
    """Return the stable case identity, independent of rule/model versions."""

    canonical = f"{company_code}|{fiscal_year}|{quarter}|{monitor_type}"
    return sha256(canonical.encode("utf-8")).hexdigest()


class SemanticCaseIdentity(BaseModel):
    """Server-owned semantic-case identity used by phase-2 and later agents."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    monitor_type: str
    canonical_source_record_id: UUID
    source_mode: str
    sap_link_status: str
    sap_observation_id: UUID | None
    risk_amount_source: str
    confidence_tier: str
    account_dictionary_version: str
    merged_into_case_id: UUID | None = None


def semantic_case_fingerprint(
    company_code: str,
    fiscal_year: int,
    monitor_type: str,
    source_mode: str,
    canonical_source_record_id: UUID,
    sap_observation_id: UUID | None,
) -> str:
    authority_id = sap_observation_id or canonical_source_record_id
    canonical = "|".join(
        (
            company_code,
            str(fiscal_year),
            monitor_type,
            source_mode,
            str(authority_id),
        )
    )
    return sha256(canonical.encode()).hexdigest()


def transition_case(current: CaseStatus, target: CaseStatus) -> CaseStatus:
    """Validate and return an allowed case status transition."""

    if target not in _ALLOWED_TRANSITIONS[current]:
        raise InvalidCaseTransition(f"case cannot transition from {current} to {target}")
    return target
