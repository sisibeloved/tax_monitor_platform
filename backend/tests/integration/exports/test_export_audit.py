from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import partial
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4

import pytest
from sqlalchemy import select

from tax_risk.application.audit import AuditService
from tax_risk.application.business_entertainment.reporting import (
    BusinessEntertainmentReportingService,
)
from tax_risk.application.exports import (
    ExportNotFound,
    ExportService,
    InMemoryExportObjectStore,
    export_authorization_version,
)
from tax_risk.config import Settings
from tax_risk.domain.exports import ExportType
from tax_risk.persistence.export_models import ExportJob
from tax_risk.persistence.ingest_models import Company, CompanyLifecycle
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory
from tax_risk.persistence.risk_models import AuditEvent
from tax_risk.security.principal import GROUP_TAX_ROLE, Principal


def test_authorization_version_is_deterministic_without_raw_scope_values() -> None:
    first = export_authorization_version(
        subject="user",
        roles=frozenset({"company-finance"}),
        company_ids=frozenset(),
    )
    second = export_authorization_version(
        subject="user",
        roles=frozenset({"company-finance"}),
        company_ids=frozenset(),
    )
    assert first == second
    assert len(first) == 64
    assert "user" not in first


def test_export_lifecycle_records_completion_download_denial_and_expiry(
    isolated_database_url: str,
) -> None:
    engine, factory = create_session_factory(isolated_database_url)
    audit = AuditService(lambda: UnitOfWork(factory))
    service = ExportService(
        partial(UnitOfWork, factory),
        BusinessEntertainmentReportingService(partial(UnitOfWork, factory)),
        InMemoryExportObjectStore(),
        audit,
        Settings(environment="test", export_download_secret="export-audit-secret"),
    )
    principal = Principal(
        subject="export-audit-owner",
        roles=frozenset({GROUP_TAX_ROLE}),
        allowed_company_ids=frozenset(),
        organization_path="/group/tax",
    )
    try:
        with factory() as session:
            session.add(
                Company(
                    company_code=f"EXPORT-AUDIT-{uuid4().hex[:8]}",
                    company_name="导出审计测试公司",
                    lifecycle=CompanyLifecycle.ACTIVE,
                )
            )
            session.commit()
        job = service.create_export(
            principal,
            export_type=ExportType.BUSINESS_ENTERTAINMENT,
            filters={},
        )
        service.complete_for_test(job.id, b"xlsx", row_count=2)
        url = service.issue_download_url(principal, job.id)
        query = parse_qs(urlsplit(url).query)
        service.download(
            principal,
            job.id,
            expires=int(query["expires"][0]),
            signature=query["signature"][0],
        )
        with pytest.raises(ExportNotFound):
            service.download(principal, job.id, expires=0, signature="invalid")
        with factory() as session:
            persisted = session.get(ExportJob, job.id)
            assert persisted is not None
            persisted.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            session.commit()
        with pytest.raises(ExportNotFound):
            service.download(
                principal,
                job.id,
                expires=int(query["expires"][0]),
                signature=query["signature"][0],
            )

        with factory() as session:
            events = tuple(
                session.scalars(
                    select(AuditEvent)
                    .where(AuditEvent.export_job_id == job.id)
                    .order_by(AuditEvent.occurred_at, AuditEvent.id)
                )
            )
        assert [event.action for event in events] == [
            "EXPORT_CREATED",
            "EXPORT_COMPLETED",
            "EXPORT_DOWNLOADED",
            "EXPORT_DOWNLOAD_DENIED",
            "EXPORT_EXPIRED",
        ]
        assert events[1].row_count == 2
        assert len(events[1].payload["checksum"]) == 64
        assert events[3].result == "DENIED"
    finally:
        engine.dispose()
