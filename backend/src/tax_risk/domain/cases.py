"""Risk-case identity and workflow invariants."""

from __future__ import annotations

from enum import StrEnum
from hashlib import sha256


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


def transition_case(current: CaseStatus, target: CaseStatus) -> CaseStatus:
    """Validate and return an allowed case status transition."""

    if target not in _ALLOWED_TRANSITIONS[current]:
        raise InvalidCaseTransition(f"case cannot transition from {current} to {target}")
    return target
