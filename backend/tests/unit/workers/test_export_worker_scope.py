from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from types import SimpleNamespace
from uuid import UUID, uuid4

from tax_risk.config import Settings
from tax_risk.security.context import current_principal
from tax_risk.security.service_scope import issue_service_scope_token
from tax_risk.workers.celery_app import create_celery_app
from tax_risk.workers.exports import RENDER_EXPORT_TASK, register_export_tasks


@dataclass
class _ScopedExportService:
    expected_company: UUID
    expected_authorization_version: str

    def render_export(
        self,
        job_id: UUID,
        *,
        authorization_version: str,
    ) -> SimpleNamespace:
        principal = current_principal()
        assert principal is not None and principal.is_service
        assert principal.allowed_company_ids == frozenset({self.expected_company})
        assert authorization_version == self.expected_authorization_version
        return SimpleNamespace(
            id=job_id,
            status="COMPLETED",
            row_count=1,
            checksum="a" * 64,
        )


def test_export_worker_verifies_and_binds_the_frozen_scope() -> None:
    company_id = uuid4()
    job_id = uuid4()
    authorization_version = "b" * 64
    settings = Settings(
        environment="test",
        redis_url="redis://localhost:6379/15",
        celery_task_always_eager=True,
        celery_task_eager_propagates=True,
        celery_task_store_eager_result=True,
        worker_scope_secret="signed-export-worker-scope-test",
    )
    app = create_celery_app(settings)
    service = _ScopedExportService(company_id, authorization_version)
    register_export_tasks(app=app, service_factory=lambda: service)  # type: ignore[arg-type]
    token = issue_service_scope_token(
        secret=settings.worker_scope_secret,
        queue="exports",
        run_type="EXPORT",
        batch_id=str(job_id),
        company_ids=frozenset({company_id}),
        period=date(2026, 6, 30),
    )

    result = (
        app.signature(
            RENDER_EXPORT_TASK,
            args=(str(job_id), authorization_version, token),
        )
        .apply_async()
        .get(timeout=10)
    )

    assert result == {
        "job_id": str(job_id),
        "status": "COMPLETED",
        "row_count": 1,
        "checksum": "a" * 64,
    }
