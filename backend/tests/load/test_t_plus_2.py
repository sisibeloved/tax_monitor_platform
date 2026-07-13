from datetime import datetime, timedelta, timezone

from tax_risk.domain.task_runs import delivery_gate


def test_exact_48_hour_boundary_passes_for_every_valid_company(
    capacity_profile,
    capacity_evidence,
) -> None:
    data_ready_at = datetime(2026, 7, 1, 8, tzinfo=timezone.utc)
    valid_company_outputs = {
        f"C{index:04d}": data_ready_at + timedelta(hours=47, minutes=30)
        for index in range(1, 124)
    }
    valid_company_outputs["C0126"] = data_ready_at + timedelta(hours=48)

    result = delivery_gate(
        data_ready_at=data_ready_at,
        valid_company_outputs=valid_company_outputs,
        failed_company_outputs={"C0124": None, "C0125": None},
        maximum_hours=capacity_profile["thresholds"]["maximum_delivery_hours"],
    )

    assert result.passed is True
    assert result.valid_company_count == 124
    assert result.on_time_company_count == 124
    assert result.false_ready_company_count == 0
    capacity_evidence["checks"]["t_plus_2"] = {
        "passed": result.passed,
        **result.model_dump(mode="json"),
    }


def test_one_second_after_48_hours_fails_even_when_success_rate_exceeds_98_percent() -> None:
    data_ready_at = datetime(2026, 7, 1, 8, tzinfo=timezone.utc)
    outputs = {
        f"C{index:04d}": data_ready_at + timedelta(hours=24)
        for index in range(1, 125)
    }
    outputs["C0126"] = data_ready_at + timedelta(hours=48, seconds=1)

    result = delivery_gate(
        data_ready_at=data_ready_at,
        valid_company_outputs=outputs,
        failed_company_outputs={},
        maximum_hours=48,
    )

    assert result.success_rate >= 0.98
    assert result.passed is False
    assert result.late_companies == ("C0126",)
