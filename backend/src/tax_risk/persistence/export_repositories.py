from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from tax_risk.persistence.export_models import ExportJob


class ExportRepository:
    def __init__(self, session: Session) -> None:
        self._session = session

    def add(self, job: ExportJob) -> None:
        self._session.add(job)

    def get(self, job_id: UUID, *, for_update: bool = False) -> ExportJob | None:
        statement = select(ExportJob).where(ExportJob.id == job_id)
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return self._session.scalar(statement)

    def list_for_requester(self, requester_subject: str) -> tuple[ExportJob, ...]:
        return tuple(
            self._session.scalars(
                select(ExportJob)
                .where(ExportJob.requester_subject == requester_subject)
                .order_by(ExportJob.created_at.desc(), ExportJob.id.desc())
            )
        )


__all__ = ["ExportRepository"]
