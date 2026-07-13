"""Scoped asynchronous export creation, rendering and download authorization."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import hmac
import json
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

from sqlalchemy import select

from tax_risk.application.audit import AuditEventDraft, AuditService, normalized_filter_hash
from tax_risk.application.business_entertainment.export import (
    EXPORT_SCHEMA_VERSION,
    build_export_rows,
    render_xlsx,
)
from tax_risk.application.business_entertainment.reporting import (
    BusinessEntertainmentReportingService,
)
from tax_risk.config import Settings
from tax_risk.observability.metrics import DEFAULT_METRICS
from tax_risk.db import apply_principal_context
from tax_risk.domain.exports import ExportJobStatus, ExportType
from tax_risk.persistence.export_models import ExportJob
from tax_risk.persistence.ingest_models import Company
from tax_risk.persistence.repositories import UnitOfWork
from tax_risk.security.policies import Action, DEFAULT_POLICY, ResourceNotFound
from tax_risk.security.principal import GROUP_TAX_ROLE, Principal


class ExportNotFound(LookupError):
    pass


class ExportNotReady(RuntimeError):
    pass


class ExportObjectStore(Protocol):
    def put(self, object_key: str, payload: bytes) -> None: ...

    def get(self, object_key: str) -> bytes: ...

    def delete(self, object_key: str) -> None: ...


class InMemoryExportObjectStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put(self, object_key: str, payload: bytes) -> None:
        self.objects[object_key] = payload

    def get(self, object_key: str) -> bytes:
        try:
            return self.objects[object_key]
        except KeyError as exc:
            raise ExportNotFound(object_key) from exc

    def delete(self, object_key: str) -> None:
        self.objects.pop(object_key, None)


class FileExportObjectStore:
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()

    def put(self, object_key: str, payload: bytes) -> None:
        destination = self._path(object_key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(payload)

    def get(self, object_key: str) -> bytes:
        path = self._path(object_key)
        if not path.is_file():
            raise ExportNotFound(object_key)
        return path.read_bytes()

    def delete(self, object_key: str) -> None:
        path = self._path(object_key)
        if path.exists():
            path.unlink()

    def _path(self, object_key: str) -> Path:
        candidate = (self._root / object_key).resolve()
        if self._root not in candidate.parents:
            raise ValueError("export object key escapes configured storage root")
        return candidate


@dataclass(frozen=True, slots=True)
class ExportJobView:
    id: UUID
    export_type: str
    requester_subject: str
    company_ids: tuple[str, ...]
    normalized_filters: dict[str, object]
    schema_version: str
    status: str
    row_count: int | None
    checksum: str | None
    object_key: str | None
    failure_code: str | None
    expires_at: datetime
    created_at: datetime
    completed_at: datetime | None


def export_authorization_version(
    *,
    subject: str,
    roles: frozenset[str],
    company_ids: frozenset[UUID],
) -> str:
    canonical = json.dumps(
        {
            "subject": subject,
            "roles": sorted(roles),
            "company_ids": sorted(str(value) for value in company_ids),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


class ExportService:
    def __init__(
        self,
        uow_factory: Callable[[], UnitOfWork],
        reporting: BusinessEntertainmentReportingService,
        object_store: ExportObjectStore,
        audit: AuditService,
        settings: Settings,
    ) -> None:
        self._uow_factory = uow_factory
        self._reporting = reporting
        self._store = object_store
        self._audit = audit
        self._settings = settings

    def create_export(
        self,
        principal: Principal,
        *,
        export_type: ExportType,
        filters: Mapping[str, Any],
    ) -> ExportJobView:
        policy_scope = DEFAULT_POLICY.company_scope(principal, Action.EXPORT_RISK)
        requested = frozenset(UUID(value) for value in filters.get("company_ids", ()))
        if requested and policy_scope is not None and not requested <= policy_scope:
            raise ResourceNotFound("requested export scope is not authorized")
        with self._uow_factory() as uow:
            apply_principal_context(uow.session, principal)
            if requested:
                frozen_scope = requested
            elif policy_scope is None:
                frozen_scope = frozenset(uow.session.scalars(select(Company.id)))
            else:
                frozen_scope = policy_scope
            normalized = _normalize_filters(filters, frozen_scope)
            job = ExportJob(
                id=uuid4(),
                export_type=export_type,
                requester_subject=principal.subject,
                requester_roles=sorted(principal.roles),
                company_ids=sorted(str(value) for value in frozen_scope),
                normalized_filters=normalized,
                filters_hash=normalized_filter_hash(normalized),
                authorization_version=export_authorization_version(
                    subject=principal.subject,
                    roles=principal.roles,
                    company_ids=frozen_scope,
                ),
                schema_version=EXPORT_SCHEMA_VERSION,
                status=ExportJobStatus.QUEUED,
                expires_at=datetime.now(timezone.utc)
                + timedelta(hours=self._settings.export_retention_hours),
            )
            uow.exports.add(job)
            uow.commit()
            view = _view(job)
        self._audit.append(
            AuditEventDraft(
                action="EXPORT_CREATED",
                entity_type="EXPORT_JOB",
                entity_id=view.id,
                principal=principal,
                company_ids=frozen_scope,
                result="SUCCEEDED",
                filters_hash=normalized_filter_hash(normalized),
                export_job_id=view.id,
            )
        )
        _record_export_metric(ExportJobStatus.QUEUED)
        return view

    def render_export(self, job_id: UUID | str) -> ExportJobView:
        resolved_id = UUID(str(job_id))
        with self._uow_factory() as uow:
            job = uow.exports.get(resolved_id, for_update=True)
            if job is None:
                raise ExportNotFound(str(resolved_id))
            if ExportJobStatus(job.status) == ExportJobStatus.COMPLETED:
                return _view(job)
            if ExportJobStatus(job.status) not in {
                ExportJobStatus.QUEUED,
                ExportJobStatus.FAILED,
            }:
                raise ExportNotReady(str(resolved_id))
            job.status = ExportJobStatus.RUNNING
            job.started_at = datetime.now(timezone.utc)
            frozen_scope = frozenset(UUID(value) for value in job.company_ids)
            filters = dict(job.normalized_filters)
            uow.commit()
        _record_export_metric(ExportJobStatus.RUNNING)

        try:
            root_cases = self._reporting.list_root_cases(
                company_scope=frozen_scope,
                fiscal_year=_optional_int(filters.get("fiscal_year")),
                period=_optional_int(filters.get("period")),
                source_mode=_optional_str(filters.get("source_mode")),
                sap_link_status=_optional_str(filters.get("sap_link_status")),
                confidence_tier=_optional_str(filters.get("confidence")),
                case_status=_optional_str(filters.get("status")),
            )
            payload = render_xlsx(build_export_rows(root_cases))
            return self._complete(resolved_id, payload, row_count=len(root_cases))
        except Exception:
            with self._uow_factory() as uow:
                failed = uow.exports.get(resolved_id, for_update=True)
                if failed is not None:
                    failed.status = ExportJobStatus.FAILED
                    failed.failure_code = "EXPORT_RENDER_FAILED"
                    failed.completed_at = datetime.now(timezone.utc)
                    uow.commit()
            _record_export_metric(ExportJobStatus.FAILED)
            raise

    def complete_for_test(
        self, job_id: UUID | str, payload: bytes, *, row_count: int
    ) -> ExportJobView:
        return self._complete(UUID(str(job_id)), payload, row_count=row_count)

    def get_export(self, principal: Principal, job_id: UUID | str) -> ExportJobView:
        job = self._authorized_job(principal, UUID(str(job_id)))
        return _view(job)

    def list_exports(self, principal: Principal) -> tuple[ExportJobView, ...]:
        DEFAULT_POLICY.require(principal, Action.EXPORT_RISK)
        with self._uow_factory() as uow:
            apply_principal_context(uow.session, principal)
            rows = uow.exports.list_for_requester(principal.subject)
            views = tuple(_view(job) for job in rows if self._is_currently_authorized(principal, job))
        return views

    def issue_download_url(self, principal: Principal, job_id: UUID | str) -> str:
        job = self._authorized_job(principal, UUID(str(job_id)))
        if ExportJobStatus(job.status) != ExportJobStatus.COMPLETED or not job.object_key:
            raise ExportNotReady(str(job.id))
        if job.expires_at <= datetime.now(timezone.utc):
            self._expire(job.id, job.object_key)
            raise ExportNotFound(str(job.id))
        expires = int(
            (datetime.now(timezone.utc) + timedelta(seconds=self._settings.export_download_ttl_seconds)).timestamp()
        )
        signature = self._download_signature(job.id, principal.subject, expires)
        return f"/api/v1/exports/{job.id}/content?expires={expires}&signature={signature}"

    def download(
        self,
        principal: Principal,
        job_id: UUID | str,
        *,
        expires: int,
        signature: str,
    ) -> bytes:
        resolved_id = UUID(str(job_id))
        if expires < int(datetime.now(timezone.utc).timestamp()) or not hmac.compare_digest(
            signature,
            self._download_signature(resolved_id, principal.subject, expires),
        ):
            raise ExportNotFound(str(resolved_id))
        job = self._authorized_job(principal, resolved_id)
        if ExportJobStatus(job.status) != ExportJobStatus.COMPLETED or not job.object_key:
            raise ExportNotFound(str(resolved_id))
        payload = self._store.get(job.object_key)
        self._audit.append(
            AuditEventDraft(
                action="EXPORT_DOWNLOADED",
                entity_type="EXPORT_JOB",
                entity_id=job.id,
                principal=principal,
                company_ids=frozenset(UUID(value) for value in job.company_ids),
                result="SUCCEEDED",
                row_count=job.row_count,
                export_job_id=job.id,
                payload={"checksum": job.checksum},
            )
        )
        return payload

    def _complete(self, job_id: UUID, payload: bytes, *, row_count: int) -> ExportJobView:
        checksum = sha256(payload).hexdigest()
        object_key = f"exports/{job_id}/{checksum}.xlsx"
        self._store.put(object_key, payload)
        with self._uow_factory() as uow:
            job = uow.exports.get(job_id, for_update=True)
            if job is None:
                self._store.delete(object_key)
                raise ExportNotFound(str(job_id))
            job.status = ExportJobStatus.COMPLETED
            job.row_count = row_count
            job.checksum = checksum
            job.object_key = object_key
            job.failure_code = None
            job.completed_at = datetime.now(timezone.utc)
            uow.commit()
            view = _view(job)
        _record_export_metric(ExportJobStatus.COMPLETED)
        return view

    def _authorized_job(self, principal: Principal, job_id: UUID) -> ExportJob:
        DEFAULT_POLICY.require(principal, Action.EXPORT_RISK)
        with self._uow_factory() as uow:
            apply_principal_context(uow.session, principal)
            job = uow.exports.get(job_id)
            if job is None or not self._is_currently_authorized(principal, job):
                raise ExportNotFound(str(job_id))
            uow.session.expunge(job)
            return job

    @staticmethod
    def _is_currently_authorized(principal: Principal, job: ExportJob) -> bool:
        if principal.subject != job.requester_subject and not principal.has_role(
            GROUP_TAX_ROLE
        ):
            return False
        current_scope = DEFAULT_POLICY.company_scope(principal, Action.EXPORT_RISK)
        frozen_scope = frozenset(UUID(value) for value in job.company_ids)
        return current_scope is None or frozen_scope <= current_scope

    def _download_signature(self, job_id: UUID, subject: str, expires: int) -> str:
        message = f"{job_id}|{subject}|{expires}".encode("utf-8")
        return hmac.new(
            self._settings.export_download_secret.encode("utf-8"),
            message,
            sha256,
        ).hexdigest()

    def _expire(self, job_id: UUID, object_key: str) -> None:
        self._store.delete(object_key)
        with self._uow_factory() as uow:
            job = uow.exports.get(job_id, for_update=True)
            if job is not None:
                job.status = ExportJobStatus.EXPIRED
                uow.commit()
        _record_export_metric(ExportJobStatus.EXPIRED)


def _record_export_metric(status: ExportJobStatus) -> None:
    DEFAULT_METRICS.metric("tax_risk_export_total").inc(
        {"status": status.value, "format": "XLSX"}
    )


def build_default_export_service(settings: Settings | None = None) -> ExportService:
    resolved = settings or Settings()
    reporting = BusinessEntertainmentReportingService(UnitOfWork)
    return ExportService(
        UnitOfWork,
        reporting,
        FileExportObjectStore(resolved.export_storage_path),
        AuditService(UnitOfWork),
        resolved,
    )


def _normalize_filters(
    filters: Mapping[str, Any], company_ids: frozenset[UUID]
) -> dict[str, object]:
    normalized: dict[str, object] = {
        key: value for key, value in filters.items() if key != "company_ids" and value is not None
    }
    normalized["company_ids"] = sorted(str(value) for value in company_ids)
    return normalized


def _view(job: ExportJob) -> ExportJobView:
    return ExportJobView(
        id=job.id,
        export_type=str(job.export_type),
        requester_subject=job.requester_subject,
        company_ids=tuple(job.company_ids),
        normalized_filters=dict(job.normalized_filters),
        schema_version=job.schema_version,
        status=str(job.status),
        row_count=job.row_count,
        checksum=job.checksum,
        object_key=job.object_key,
        failure_code=job.failure_code,
        expires_at=job.expires_at,
        created_at=job.created_at,
        completed_at=job.completed_at,
    )


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError("boolean is not a valid integer export filter")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        return int(value)
    raise ValueError("export integer filter must be an integer or string")


def _optional_str(value: object) -> str | None:
    return str(value) if value is not None else None


__all__ = [
    "ExportJobView",
    "ExportNotFound",
    "ExportNotReady",
    "ExportObjectStore",
    "ExportService",
    "FileExportObjectStore",
    "InMemoryExportObjectStore",
    "build_default_export_service",
    "export_authorization_version",
]
