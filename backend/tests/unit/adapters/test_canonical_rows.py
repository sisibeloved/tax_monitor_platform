from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from tax_risk.adapters.ingest.base import CanonicalFinancialRow, CompanyMasterRow


def _financial_row(**overrides: object) -> CanonicalFinancialRow:
    values: dict[str, object] = {
        "source_record_key": "row-1",
        "company_code": "C001",
        "fiscal_year": 2026,
        "period": date(2026, 3, 31),
        "currency": "CNY",
        "amount_scale": 2,
        "metric_code": "cumulative_profit",
        "amount": Decimal("10.00"),
        "extracted_at": datetime(2026, 4, 1, 8, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return CanonicalFinancialRow(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "overrides",
    [
        {"source_record_key": ""},
        {"source_record_key": "x" * 513},
        {"company_code": " "},
        {"company_code": "x" * 65},
        {"fiscal_year": True},
        {"fiscal_year": 1999},
        {"fiscal_year": 2025},
        {"period": datetime(2026, 3, 31, tzinfo=timezone.utc)},
        {"currency": "cny"},
        {"currency": "CN"},
        {"amount_scale": True},
        {"amount_scale": -1},
        {"amount_scale": 13},
        {"metric_code": ""},
        {"metric_code": "x" * 129},
        {"amount": Decimal("NaN")},
        {"amount": Decimal("1.001")},
        {"amount": Decimal("100000000000000000000000000.00")},
        {"extracted_at": datetime(2026, 4, 1, 8)},
    ],
)
def test_financial_canonical_row_rejects_runtime_contract_violations(
    overrides: dict[str, object],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        _financial_row(**overrides)


def _company_row(**overrides: object) -> CompanyMasterRow:
    values: dict[str, object] = {
        "source_record_key": "master-1",
        "company_code": "C001",
        "company_name": "Company One",
        "lifecycle": "ACTIVE",
        "extracted_at": datetime(2026, 4, 1, 8, tzinfo=timezone.utc),
    }
    values.update(overrides)
    return CompanyMasterRow(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "overrides",
    [
        {"source_record_key": ""},
        {"source_record_key": "x" * 513},
        {"company_code": ""},
        {"company_code": "x" * 65},
        {"company_name": " "},
        {"company_name": "x" * 257},
        {"lifecycle": "DELETED"},
        {"extracted_at": datetime(2026, 4, 1, 8)},
    ],
)
def test_company_master_canonical_row_rejects_runtime_contract_violations(
    overrides: dict[str, object],
) -> None:
    with pytest.raises((TypeError, ValueError)):
        _company_row(**overrides)
