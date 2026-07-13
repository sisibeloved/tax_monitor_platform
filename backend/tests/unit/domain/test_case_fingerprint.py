from hashlib import sha256
from importlib import import_module


def _fingerprint(
    company_code: str,
    fiscal_year: int,
    quarter: int,
    monitor_type: str,
) -> str:
    cases = import_module("tax_risk.domain.cases")
    return cases.case_fingerprint(company_code, fiscal_year, quarter, monitor_type)


def test_case_fingerprint_is_the_approved_numeric_identity() -> None:
    expected = sha256(b"C001|2026|2|ACCRUAL_ACCURACY").hexdigest()

    assert _fingerprint("C001", 2026, 2, "ACCRUAL_ACCURACY") == expected


def test_case_fingerprint_changes_by_quarter_and_monitor_only() -> None:
    identities = {
        _fingerprint("C001", 2026, 1, "ACCRUAL_ACCURACY"),
        _fingerprint("C001", 2026, 2, "ACCRUAL_ACCURACY"),
        _fingerprint("C001", 2026, 2, "TAX_BURDEN"),
        _fingerprint("C001", 2026, 2, "POTENTIAL_TAX_COST"),
    }

    assert len(identities) == 4
