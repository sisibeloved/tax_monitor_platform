from decimal import Decimal

import pytest

from tax_risk.domain.cases import MonitorType
from tax_risk.domain.semantic.limited_scope import (
    MissingScopeInput,
    ScopeInput,
    evaluate_scope,
)


@pytest.mark.parametrize(
    ("expense", "base", "rate", "selected", "adjustment"),
    [
        ("140.00", "1000.00", "0.14", False, "0.0000"),
        ("140.01", "1000.00", "0.14", True, "0.0100"),
        ("120.00", "1000.00", "0.12", False, "0.0000"),
        ("120.01", "1000.00", "0.12", True, "0.0100"),
        ("0.00", "-100.00", "0.12", True, "12.0000"),
    ],
)
def test_scope_is_strictly_greater_than_zero(
    expense: str,
    base: str,
    rate: str,
    selected: bool,
    adjustment: str,
) -> None:
    result = evaluate_scope(
        ScopeInput(
            company_code="1001",
            period="2026-06",
            cumulative_expense=Decimal(expense),
            cumulative_base=Decimal(base),
            limit_rate=Decimal(rate),
        )
    )

    assert result.selected is selected
    assert result.adjustment == Decimal(adjustment)


@pytest.mark.parametrize("field", ["cumulative_expense", "cumulative_base"])
def test_missing_scope_value_is_not_treated_as_zero(field: str) -> None:
    values: dict[str, Decimal | None] = {
        "cumulative_expense": Decimal("10"),
        "cumulative_base": Decimal("100"),
    }
    values[field] = None

    with pytest.raises(MissingScopeInput, match=field):
        evaluate_scope(
            ScopeInput(
                company_code="1001",
                period="2026-06",
                cumulative_expense=values["cumulative_expense"],
                cumulative_base=values["cumulative_base"],
                limit_rate=Decimal("0.14"),
            )
        )


def test_phase_3_monitor_types_are_explicit() -> None:
    assert MonitorType.WELFARE.value == "WELFARE"
    assert MonitorType.DONATION.value == "DONATION"
