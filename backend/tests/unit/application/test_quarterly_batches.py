from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from tax_risk.application.quarterly_batches import QuarterlyBatchError, QuarterlyBatchService
from tax_risk.persistence.risk_models import MonitoringRunStatus


class _RecordingRisks:
    def __init__(self) -> None:
        self.list_called = False
        self.run = SimpleNamespace(
            id=uuid4(),
            run_key="quarterly:2026:Q2:set:rule",
            status=MonitoringRunStatus.RUNNING,
        )

    def get_run(self, run_id, *, for_update: bool = False):
        del run_id, for_update
        return self.run

    def list_run_companies(self, *args, **kwargs):
        del args, kwargs
        self.list_called = True
        return []


class _FakeUow:
    def __init__(self, risks: _RecordingRisks) -> None:
        self.risks = risks

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        del args


def test_retry_failed_requires_a_terminal_failed_or_partial_batch() -> None:
    risks = _RecordingRisks()
    service = QuarterlyBatchService(lambda: _FakeUow(risks))  # type: ignore[arg-type]

    with pytest.raises(QuarterlyBatchError) as caught:
        service.retry_failed(run_id=risks.run.id)

    assert caught.value.error_code == "QUARTERLY_BATCH_RETRY_NOT_ALLOWED"
    assert risks.list_called is False
