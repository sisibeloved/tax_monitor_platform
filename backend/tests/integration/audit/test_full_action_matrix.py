from datetime import date, datetime, timezone
from functools import partial
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import select

from tax_risk.application.audit import AuditService
from tax_risk.application.exports import InMemoryExportObjectStore
from tax_risk.config import Settings
from tax_risk.main import create_app
from tax_risk.persistence.models import ReleaseEvent
from tax_risk.persistence.ingest_models import Company, CompanyLifecycle
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory
from tax_risk.persistence.risk_models import AuditEvent
from tax_risk.release.reporting import SqlReleaseStore
from tax_risk.release.rollback import (
    DatabaseRollbackAuditSink,
    DeterministicDrillOperations,
    RollbackRunner,
)
from tax_risk.release.signing import SignatureEnvelope
from tax_risk.security.principal import GROUP_TAX_ROLE, Principal
from tax_risk.security.service_scope import issue_service_scope_token
from tax_risk.workers.celery_app import create_celery_app
from tax_risk.workers.exports import RENDER_EXPORT_TASK, register_export_tasks

from tests.integration.release.test_release_audit import _manifest
from tests.integration.release.test_rollback_drill import _inputs


def _principal(subject: str) -> Principal:
    return Principal(
        subject=subject,
        roles=frozenset({GROUP_TAX_ROLE}),
        allowed_company_ids=frozenset(),
        organization_path="/group/tax",
    )


def test_full_sensitive_action_matrix_uses_real_http_and_lifecycle_services(
    isolated_database_url: str,
    tmp_path: Path,
) -> None:
    engine, factory = create_session_factory(isolated_database_url)
    current = {"principal": _principal("matrix-maker")}
    settings = Settings(
        environment="test",
        export_download_secret="full-action-matrix-secret",
        worker_scope_secret="full-action-matrix-worker-scope-secret",
        redis_url="redis://localhost:6379/15",
        celery_task_always_eager=True,
        celery_task_eager_propagates=True,
        celery_task_store_eager_result=True,
    )
    dispatched: list[dict[str, object]] = []
    app = create_app(
        uow_factory=partial(UnitOfWork, factory),
        settings=settings,
        principal_provider=lambda _request: current["principal"],
        export_dispatcher=lambda **payload: dispatched.append(payload),
        export_object_store=InMemoryExportObjectStore(),
    )
    try:
        with factory() as session:
            session.add(
                Company(
                    company_code=f"MATRIX-{uuid4().hex[:8]}",
                    company_name="审计矩阵测试公司",
                    lifecycle=CompanyLifecycle.ACTIVE,
                )
            )
            session.commit()
        with TestClient(app) as client:
            assert client.get(
                "/api/v1/risk-cases?fiscal_year=2026&quarter=2"
            ).status_code == 200
            missing_case = uuid4()
            assert client.get(f"/api/v1/risk-cases/{missing_case}").status_code == 404
            assert client.post(
                f"/api/v1/risk-cases/{missing_case}/actions",
                json={"action": "CLOSE", "to_status": "CLOSED", "reason": "复核完成"},
            ).status_code in {404, 422}

            ingest = client.post(
                "/api/v1/ingest-batches",
                json={
                    "source": "SAP",
                    "source_batch_key": f"audit-matrix-{uuid4()}",
                    "dataset_code": "quarterly_metric",
                    "extraction_time": "2026-07-13T08:00:00Z",
                    "period": "2026-06-30",
                    "mode": "FULL",
                    "schema_version": "audit-matrix-v1",
                    "currency": "CNY",
                    "amount_scale": 2,
                },
            )
            assert ingest.status_code == 201, ingest.text
            assert client.post("/api/v1/tax-master/import").status_code == 422
            assert client.post("/api/v1/snapshots/validate", json={}).status_code == 422
            assert client.get("/api/v1/operations/summary").status_code == 200

            artifact_ids: list[str] = []
            for index, artifact_type in enumerate(("MODEL", "PROMPT", "CASE_LIBRARY")):
                current["principal"] = _principal(f"matrix-maker-{index}")
                created = client.post(
                    "/api/v1/semantic-artifacts",
                    json={
                        "artifact_type": artifact_type,
                        "version": f"matrix-{artifact_type.lower()}-{uuid4()}",
                        "checksum": f"{index + 1}" * 64,
                        "storage_ref": f"governed/{artifact_type.lower()}/matrix",
                        "deployment_id": "matrix-deployment" if artifact_type == "MODEL" else None,
                        "effective_from": "2038-01-01",
                        "effective_to": "2038-12-31",
                    },
                )
                assert created.status_code == 201, created.text
                artifact_id = created.json()["artifact_id"]
                artifact_ids.append(artifact_id)
                current["principal"] = _principal(f"matrix-reviewer-{index}")
                approved = client.post(
                    f"/api/v1/semantic-artifacts/{artifact_id}/approve",
                    json={"reason": "验收复核"},
                )
                assert approved.status_code == 200, approved.text
                current["principal"] = _principal(f"matrix-publisher-{index}")
                published = client.post(
                    f"/api/v1/semantic-artifacts/{artifact_id}/publish",
                    json={"reason": "验收发布"},
                )
                assert published.status_code == 200, published.text

            current["principal"] = _principal("matrix-exporter")
            created_export = client.post(
                "/api/v1/exports",
                json={"export_type": "BUSINESS_ENTERTAINMENT", "filters": {}},
            )
            assert created_export.status_code == 202, created_export.text
            export_id = created_export.json()["id"]
            assert len(dispatched) == 1
            company_ids = frozenset(
                UUID(value) for value in created_export.json()["company_ids"]
            )
            worker = create_celery_app(settings)
            register_export_tasks(
                app=worker,
                service_factory=lambda: app.state.export_service,
            )
            scope_token = issue_service_scope_token(
                secret=settings.worker_scope_secret,
                queue="exports",
                run_type="EXPORT",
                batch_id=export_id,
                company_ids=company_ids,
                period=date(2026, 7, 13),
            )
            worker.signature(
                RENDER_EXPORT_TASK,
                args=(
                    export_id,
                    str(dispatched[0]["authorization_version"]),
                    scope_token,
                ),
            ).apply_async().get(timeout=10)
            download_url = client.post(f"/api/v1/exports/{export_id}/download-url")
            assert download_url.status_code == 200, download_url.text
            assert client.get(urlsplit(download_url.json()["url"]).path + "?" + urlsplit(download_url.json()["url"]).query).status_code == 200

        release_store = SqlReleaseStore(partial(UnitOfWork, factory))
        manifest = _manifest().model_copy(
            update={"candidate_version": f"full-action-matrix-{uuid4()}"}
        )
        release_id = release_store.create_candidate(manifest, actor="matrix-release-bot")
        release_store.record_replay_started(release_id, actor="matrix-release-bot")
        release_store.record_replay_result(
            release_id,
            report_sha256="7" * 64,
            approved=True,
            actor="matrix-release-bot",
        )
        release_store.approve(release_id, approver="matrix-tax-owner")
        release_store.attach_signature(
            release_id,
            SignatureEnvelope(
                manifest_sha256=manifest.manifest_sha256,
                key_id="matrix-kms-key",
                key_version="v1",
                signature_base64="bWF0cml4LXNpZ25hdHVyZQ==",
                signed_at=datetime.now(timezone.utc),
            ),
            actor="matrix-kms-workload",
        )
        release_store.record_verification(release_id, actor="matrix-verifier")
        release_store.promote(release_id, approver="matrix-operations-owner")

        rollback_principal = _principal("matrix-rollback-controller")
        rollback_report = RollbackRunner(
            DeterministicDrillOperations(),
            audit_sink=DatabaseRollbackAuditSink(
                AuditService(lambda: UnitOfWork(factory)),
                rollback_principal,
            ),
        ).run(_inputs(tmp_path))

        with factory() as session:
            audit_events = tuple(session.scalars(select(AuditEvent)))
            release_events = tuple(
                session.scalars(
                    select(ReleaseEvent).where(ReleaseEvent.manifest_id == release_id)
                )
            )
        actions = {event.action for event in audit_events}
        assert {
            "HTTP_RISK_CASE_LIST",
            "HTTP_RISK_CASE_DETAIL",
            "HTTP_RISK_CASE_ACTION",
            "HTTP_POST_INGEST_BATCHES",
            "HTTP_POST_TAX_MASTER_IMPORT",
            "HTTP_POST_SNAPSHOTS_VALIDATE",
            "HTTP_GET_OPERATIONS_SUMMARY",
            "HTTP_POST_SEMANTIC_ARTIFACTS",
            "EXPORT_CREATED",
            "EXPORT_COMPLETED",
            "EXPORT_DOWNLOADED",
            "ROLLBACK_MANIFEST_SWITCHED",
            "ROLLBACK_CHECKSUM_VERIFIED",
            "ROLLBACK_REPRESENTATIVE_RERUN",
            "ROLLBACK_RECOVERY_DECIDED",
        } <= actions
        assert sum(action.endswith("_APPROVE") for action in actions) >= len(artifact_ids)
        assert sum(action.endswith("_PUBLISH") for action in actions) >= len(artifact_ids)
        assert all(
            event.request_id and event.filters_hash
            for event in audit_events
            if event.action.startswith("HTTP_")
        )
        assert rollback_report.recovery_verified is True

        release_actions = {event.action for event in release_events}
        assert release_actions == {
            "CANDIDATE_CREATED",
            "REPLAY_STARTED",
            "REPLAY_APPROVED",
            "RELEASE_APPROVED",
            "MANIFEST_SIGNED",
            "SIGNATURE_VERIFIED",
            "RELEASE_PROMOTED",
        }
        assert all(event.manifest_sha256 == manifest.manifest_sha256 for event in release_events)
    finally:
        engine.dispose()
