from __future__ import annotations

from types import TracebackType
from typing import Self

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from tax_risk.config import Settings
from tax_risk.persistence.business_entertainment_repositories import (
    BusinessEntertainmentScopeRepository,
)
from tax_risk.persistence.ingest_repositories import IngestRepository
from tax_risk.persistence.master_repositories import MasterRepository
from tax_risk.persistence.risk_repositories import RiskRepository
from tax_risk.persistence.semantic_repositories import SemanticRepository
from tax_risk.persistence.snapshot_repositories import SnapshotRepository


def create_session_factory(database_url: str) -> tuple[Engine, sessionmaker[Session]]:
    """Create the one engine/session-factory boundary used by application composition."""

    database_engine = create_engine(database_url, pool_pre_ping=True)
    factory = sessionmaker(
        bind=database_engine,
        class_=Session,
        autoflush=False,
        expire_on_commit=False,
    )
    return database_engine, factory


engine, session_factory = create_session_factory(Settings().database_url)


class UnitOfWork:
    """Canonical transaction boundary composing focused repositories."""

    def __init__(self, factory: sessionmaker[Session] = session_factory) -> None:
        self._session_factory = factory
        self.session: Session
        self.ingest: IngestRepository
        self.business_entertainment_scope: BusinessEntertainmentScopeRepository
        self.master: MasterRepository
        self.snapshots: SnapshotRepository
        self.risks: RiskRepository
        self.semantic: SemanticRepository

    def __enter__(self) -> Self:
        self.session = self._session_factory()
        self.ingest = IngestRepository(self.session)
        self.business_entertainment_scope = BusinessEntertainmentScopeRepository(self.session)
        self.master = MasterRepository(self.session)
        self.snapshots = SnapshotRepository(self.session)
        self.risks = RiskRepository(self.session)
        self.semantic = SemanticRepository(self.session)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        if self.session.in_transaction():
            self.session.rollback()
        self.session.close()

    def commit(self) -> None:
        self.session.commit()

    def rollback(self) -> None:
        self.session.rollback()


__all__ = ["UnitOfWork", "create_session_factory", "engine", "session_factory"]
