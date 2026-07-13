from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from tax_risk.persistence.snapshot_models import (
    AccountingSnapshot,
    SnapshotSource,
    SnapshotSet,
    SnapshotSetMember,
)


class SnapshotRepository:
    """Snapshot and complete-set persistence operations within the caller transaction."""

    def __init__(self, session: Session) -> None:
        self._session = session

    def add_snapshot(self, snapshot: AccountingSnapshot) -> None:
        self._session.add(snapshot)

    def add_source(self, source: SnapshotSource) -> None:
        self._session.add(source)

    def get_snapshot(
        self,
        snapshot_id: UUID,
        *,
        for_update: bool = False,
    ) -> AccountingSnapshot | None:
        statement = select(AccountingSnapshot).where(AccountingSnapshot.id == snapshot_id)
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return self._session.scalar(statement)

    def find_snapshot(
        self,
        company_id: UUID,
        period: date,
        source_version_set_hash: str,
        *,
        for_update: bool = False,
    ) -> AccountingSnapshot | None:
        statement = select(AccountingSnapshot).where(
            AccountingSnapshot.company_id == company_id,
            AccountingSnapshot.period == period,
            AccountingSnapshot.source_version_set_hash == source_version_set_hash,
        )
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return self._session.scalar(statement)

    def list_snapshots(
        self,
        snapshot_ids: Iterable[UUID],
        *,
        for_update: bool = False,
    ) -> list[AccountingSnapshot]:
        ordered_ids = sorted(set(snapshot_ids))
        if not ordered_ids:
            return []
        statement = (
            select(AccountingSnapshot)
            .where(AccountingSnapshot.id.in_(ordered_ids))
            .order_by(AccountingSnapshot.id)
        )
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return list(self._session.scalars(statement))

    def list_snapshots_for_company_period(
        self,
        company_id: UUID,
        period: date,
        *,
        for_update: bool = False,
    ) -> list[AccountingSnapshot]:
        statement = (
            select(AccountingSnapshot)
            .where(
                AccountingSnapshot.company_id == company_id,
                AccountingSnapshot.period == period,
            )
            .order_by(AccountingSnapshot.id)
        )
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return list(self._session.scalars(statement))

    def list_sources(
        self,
        snapshot_ids: Iterable[UUID],
        *,
        for_update: bool = False,
    ) -> list[SnapshotSource]:
        ordered_ids = sorted(set(snapshot_ids))
        if not ordered_ids:
            return []
        statement = (
            select(SnapshotSource)
            .where(SnapshotSource.snapshot_id.in_(ordered_ids))
            .order_by(SnapshotSource.snapshot_id, SnapshotSource.ingest_batch_id, SnapshotSource.id)
        )
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return list(self._session.scalars(statement))

    def add_snapshot_set(self, snapshot_set: SnapshotSet) -> None:
        self._session.add(snapshot_set)

    def add_member(self, member: SnapshotSetMember) -> None:
        self._session.add(member)

    def get_snapshot_set_by_key(
        self,
        set_key: str,
        *,
        for_update: bool = False,
    ) -> SnapshotSet | None:
        statement = select(SnapshotSet).where(SnapshotSet.set_key == set_key)
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return self._session.scalar(statement)

    def get_snapshot_set(
        self,
        snapshot_set_id: UUID,
        *,
        for_update: bool = False,
    ) -> SnapshotSet | None:
        statement = select(SnapshotSet).where(SnapshotSet.id == snapshot_set_id)
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return self._session.scalar(statement)

    def get_snapshot_set_for_update(self, snapshot_set_id: UUID) -> SnapshotSet | None:
        statement = select(SnapshotSet).where(SnapshotSet.id == snapshot_set_id).with_for_update()
        return self._session.scalar(statement)


__all__ = ["SnapshotRepository"]
