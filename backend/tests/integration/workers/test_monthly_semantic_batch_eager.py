from __future__ import annotations

from functools import partial

from sqlalchemy import text

from tax_risk.application.monthly_semantic_runs import MonthlySemanticRunService
from tax_risk.application.semantic.sap_voucher_monitor import MonitorRunResult
from tax_risk.domain.cases import MonitorType
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory
from tests.integration.api.test_monthly_semantic_routes import (
    _cleanup_monthly_semantic_state,
    _seed_semantic_versions,
)
from tests.integration.persistence.test_monthly_semantic_repository import _seed_monthly_set


class FakeMonitor:
    def __init__(self, failures: set[str]) -> None:
        self.failures = failures

    async def run(
        self,
        company_code: str,
        _period: str,
        _snapshot_set_id,
        _snapshot_id,
    ) -> MonitorRunResult:
        if company_code in self.failures:
            raise TimeoutError("model timeout")
        return MonitorRunResult(
            status="COMPLETED",
            selected=True,
            adjustment="1.00",
            processed_lines=1,
            created_or_updated_cases=1,
            evidence_task_count=0,
        )


def test_company_failure_is_isolated_and_only_failed_company_is_retried(
    isolated_database_url: str,
) -> None:
    engine, factory = create_session_factory(isolated_database_url)
    failures: set[str] = set()

    def monitor_factory(**_kwargs):
        return FakeMonitor(failures)

    try:
        snapshot_set_id, _, first_code = _seed_monthly_set(engine)
        second_code = f"{first_code[:-3]}001"
        failures.add(second_code)
        service = MonthlySemanticRunService(
            partial(UnitOfWork, factory),
            monitor_factory=monitor_factory,
        )
        plan = service.start_run(
            monitoring_type=MonitorType.WELFARE,
            period="2026-06",
            company_codes=(first_code, second_code),
            snapshot_set_id=snapshot_set_id,
            semantic_version_set_id=_seed_semantic_versions(engine),
            allowed_company_ids=None,
        )

        outcomes = [
            service.run_company(run_company_id=row_id, task_id=f"task-{index}")
            for index, row_id in enumerate(plan.run_company_ids)
        ]
        summary = service.summarize(plan.run.run_id)

        assert {outcome["status"] for outcome in outcomes} == {"SUCCEEDED", "FAILED"}
        assert summary["status"] == "PARTIAL_SUCCESS"
        assert summary["succeeded"] == 1 and summary["failed"] == 1
        with engine.connect() as connection:
            partial_delivery = connection.execute(
                text(
                    "SELECT batch_finished_at, output_ready_at "
                    "FROM monitoring_run WHERE id = :run_id"
                ),
                {"run_id": plan.run.run_id},
            ).one()
            company_delivery = connection.execute(
                text(
                    "SELECT status, company_output_ready_at "
                    "FROM monitoring_run_company WHERE run_id = :run_id"
                ),
                {"run_id": plan.run.run_id},
            ).all()
        assert partial_delivery.batch_finished_at is not None
        assert partial_delivery.output_ready_at is None
        assert all(
            (ready_at is not None) == (status == "SUCCEEDED")
            for status, ready_at in company_delivery
        )

        retry_ids = service.retry_failed(plan.run.run_id)
        assert len(retry_ids) == 1
        failures.clear()
        retried = service.run_company(run_company_id=retry_ids[0], task_id="retry-task")
        final = service.summarize(plan.run.run_id)

        assert retried["status"] == "SUCCEEDED"
        assert final["status"] == "SUCCEEDED"
        assert final["succeeded"] == 2 and final["failed"] == 0
        with engine.connect() as connection:
            final_delivery = connection.execute(
                text(
                    "SELECT batch_finished_at, output_ready_at "
                    "FROM monitoring_run WHERE id = :run_id"
                ),
                {"run_id": plan.run.run_id},
            ).one()
        assert final_delivery.batch_finished_at is not None
        assert final_delivery.output_ready_at is not None
    finally:
        _cleanup_monthly_semantic_state(engine)
        engine.dispose()
