from tax_risk.domain.task_runs import replay_acceptance_result


def test_replay_keeps_one_risk_candidate_evidence_task_and_effective_amount(
    capacity_profile,
    capacity_evidence,
) -> None:
    result = replay_acceptance_result(capacity_profile, replay_count=2)

    assert result.risk_fingerprint_duplicates == 0
    assert result.task_key_duplicates == 0
    assert result.effective_amount_duplicates == 0
    assert result.controlled_version_change_creates_new_run is True
    assert result.stable_risk_fingerprint_unchanged is True
    capacity_evidence["checks"]["replay_idempotency"] = {
        "passed": result.duplicate_exposure_count == 0,
        **result.model_dump(mode="json"),
    }
