from tax_risk.domain.task_runs import simulate_capacity_schedule


def test_fixed_126_company_profile_meets_reference_capacity(
    capacity_profile,
    capacity_evidence,
) -> None:
    assert capacity_profile["company_count"] == 126
    assert capacity_profile["quarterly_snapshots_per_company"] == 1
    assert capacity_profile["monthly_lines_per_company"]["total"] == 1000
    assert capacity_profile["reference_runner"] == {
        "cpu_count": 8,
        "memory_gib": 16,
        "worker_count": 16,
    }

    schedule = simulate_capacity_schedule(capacity_profile)

    assert schedule.company_count == 126
    assert schedule.task_count == 504
    assert schedule.line_count == 126_000
    assert schedule.elapsed_hours <= 24
    capacity_evidence["profile"] = capacity_profile
    capacity_evidence["checks"]["capacity"] = {
        "passed": schedule.elapsed_hours <= 24,
        **schedule.model_dump(mode="json"),
    }
