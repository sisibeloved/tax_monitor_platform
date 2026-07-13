from datetime import datetime, timezone

from tax_risk.observability.delivery import derive_batch_delivery


def _time(hour: int) -> datetime:
    return datetime(2026, 7, 13, hour, tzinfo=timezone.utc)


def test_all_successful_company_outputs_publish_batch_output_at_latest_company() -> None:
    delivery = derive_batch_delivery(
        (("SUCCEEDED", _time(8)), ("SUCCEEDED", _time(9))),
        now=_time(10),
    )

    assert delivery.batch_finished_at == _time(10)
    assert delivery.output_ready_at == _time(9)


def test_partial_success_finishes_batch_but_never_claims_batch_output_ready() -> None:
    delivery = derive_batch_delivery(
        (("SUCCEEDED", _time(8)), ("FAILED", None)),
        now=_time(10),
    )

    assert delivery.batch_finished_at == _time(10)
    assert delivery.output_ready_at is None


def test_active_company_keeps_both_batch_delivery_timestamps_empty() -> None:
    delivery = derive_batch_delivery(
        (("SUCCEEDED", _time(8)), ("RETRY_PENDING", None)),
        now=_time(10),
    )

    assert delivery.batch_finished_at is None
    assert delivery.output_ready_at is None
