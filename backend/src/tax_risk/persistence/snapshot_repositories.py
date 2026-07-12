from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from tax_risk.persistence.snapshot_models import (
    AccountingSnapshot,
    SnapshotSet,
    SnapshotSetMember,
)


class SnapshotRepository:
    """Snapshot and complete-set persistence operations within the caller transaction."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add_snapshot(self, snapshot: AccountingSnapshot) -> None:
        self._session.add(snapshot)

    def add_snapshot_set(self, snapshot_set: SnapshotSet) -> None:
        self._session.add(snapshot_set)

    def add_member(self, member: SnapshotSetMember) -> None:
        self._session.add(member)

    def get_snapshot_set_for_update(self, snapshot_set_id: UUID) -> SnapshotSet | None:
        statement = select(SnapshotSet).where(SnapshotSet.id == snapshot_set_id).with_for_update()
        return self._session.scalar(statement)


__all__ = ["SnapshotRepository"]
