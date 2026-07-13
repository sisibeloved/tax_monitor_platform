from importlib import import_module

import pytest


def _cases():
    return import_module("tax_risk.domain.cases")


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("NEW", "ASSIGNED"),
        ("ASSIGNED", "PENDING_COMPANY_CONFIRMATION"),
        ("PENDING_COMPANY_CONFIRMATION", "PENDING_ADJUSTMENT"),
        ("PENDING_ADJUSTMENT", "ADJUSTED_PENDING_REVIEW"),
        ("ADJUSTED_PENDING_REVIEW", "CLOSED"),
        ("PENDING_COMPANY_CONFIRMATION", "GROUP_REVIEW"),
        ("GROUP_REVIEW", "CLOSED"),
        ("PENDING_COMPANY_CONFIRMATION", "EVIDENCE_REQUIRED"),
        ("EVIDENCE_REQUIRED", "PENDING_COMPANY_CONFIRMATION"),
    ],
)
def test_allowed_case_transitions_follow_the_review_branches(
    current: str,
    target: str,
) -> None:
    cases = _cases()

    assert cases.transition_case(cases.CaseStatus(current), cases.CaseStatus(target)) == target


@pytest.mark.parametrize(
    ("current", "target"),
    [
        ("NEW", "CLOSED"),
        ("ASSIGNED", "GROUP_REVIEW"),
        ("PENDING_ADJUSTMENT", "GROUP_REVIEW"),
        ("CLOSED", "NEW"),
        ("CLOSED", "CLOSED"),
    ],
)
def test_illegal_case_transitions_fail(current: str, target: str) -> None:
    cases = _cases()

    with pytest.raises(cases.InvalidCaseTransition):
        cases.transition_case(cases.CaseStatus(current), cases.CaseStatus(target))
