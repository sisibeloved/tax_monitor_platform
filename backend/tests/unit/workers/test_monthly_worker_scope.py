from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from tax_risk.config import Settings
from tax_risk.security.service_scope import ServiceScopeTokenError
from tax_risk.workers.celery_app import create_celery_app
from tax_risk.workers.monthly_semantic import RUN_COMPANY_TASK, register_monthly_tasks


class _NoAccessService:
    called = False

    def run_company(self, *, run_company_id: UUID, task_id: str) -> dict[str, object]:
        del run_company_id, task_id
        self.called = True
        return {}

    def summarize(self, run_id: UUID) -> dict[str, object]:
        del run_id
        self.called = True
        return {}


def test_production_monthly_worker_rejects_unsigned_payload_before_data_access() -> None:
    service = _NoAccessService()
    app = create_celery_app(
        Settings(
            environment="production",
            redis_url="redis://localhost:6379/15",
            celery_task_always_eager=True,
            celery_task_eager_propagates=True,
            celery_task_store_eager_result=True,
            export_download_secret="test-production-export-secret-32-chars",
            worker_scope_secret="test-production-worker-secret-32-chars",
        )
    )
    register_monthly_tasks(app=app, service_factory=lambda: service)

    with pytest.raises(ServiceScopeTokenError):
        app.signature(RUN_COMPANY_TASK, args=(str(uuid4()),)).apply_async().get()

    assert service.called is False
