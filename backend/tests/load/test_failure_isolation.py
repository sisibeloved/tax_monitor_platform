from tax_risk.domain.task_runs import simulate_failure_isolation


def test_data_and_master_blocks_are_excluded_but_provider_failure_is_recovered(
    capacity_profile,
    capacity_evidence,
) -> None:
    report = simulate_failure_isolation(capacity_profile)

    assert report.total_companies == 126
    assert report.blocked_companies == 2
    assert report.valid_companies == 124
    assert report.succeeded_companies == 124
    assert report.technical_failures == 0
    assert report.retry_count == 1
    assert report.success_rate >= 0.98
    assert report.isolated_failure_count == 3
    capacity_evidence["checks"]["failure_isolation"] = {
        "passed": report.success_rate >= 0.98 and report.technical_failures == 0,
        **report.model_dump(mode="json"),
    }
