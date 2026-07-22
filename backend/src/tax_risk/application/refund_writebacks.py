"""Transactional delivery service for income-tax refund writebacks."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from math import ceil, isfinite
from typing import Protocol, runtime_checkable
from uuid import UUID

from sqlalchemy import and_, or_, select
from sqlalchemy.sql.elements import ColumnElement

from tax_risk.persistence.income_tax_refund_models import (
    IncomeTaxRefundTarget,
    IncomeTaxRefundWriteback,
)
from tax_risk.persistence.ingest_models import Company
from tax_risk.persistence.repositories import UnitOfWork


UowFactory = Callable[[], UnitOfWork]


class RefundWritebackSender(Protocol):
    """Port implemented by the Lark Base adapter."""

    def write_status(self, company_code: str, desired_value: str) -> object: ...


@runtime_checkable
class RefundWritebackSchemaPreflight(Protocol):
    """Optional capability implemented by schema-aware external writers."""

    def ensure_schema(self) -> object: ...


@dataclass(frozen=True, slots=True)
class RefundWritebackDispatchItem:
    """The minimum signed scope needed to enqueue one outbox row."""

    writeback_id: UUID
    company_id: UUID
    scope_period: date


@dataclass(frozen=True, slots=True)
class RefundWritebackDelivery:
    """Stable worker-facing result for one delivery attempt."""

    writeback_id: UUID
    company_id: UUID | None
    company_code: str | None
    status: str
    attempt_count: int
    claimed: bool
    retryable: bool
    error_code: str | None = None
    retry_after_seconds: int | None = None

    def to_payload(self) -> dict[str, object]:
        return {
            "writeback_id": str(self.writeback_id),
            "company_id": str(self.company_id) if self.company_id is not None else None,
            "company_code": self.company_code,
            "status": self.status,
            "attempt_count": self.attempt_count,
            "claimed": self.claimed,
            "retryable": self.retryable,
            "error_code": self.error_code,
            "retry_after_seconds": self.retry_after_seconds,
        }


@dataclass(frozen=True, slots=True)
class _Claim:
    writeback_id: UUID
    company_id: UUID
    company_code: str
    desired_value: str
    attempt_count: int


class IncomeTaxRefundWritebackService:
    """Claim and deliver outbox rows without holding a lock across the network call."""

    def __init__(
        self,
        uow_factory: UowFactory,
        sender: RefundWritebackSender,
        *,
        max_retries: int,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be nonnegative")
        self._uow_factory = uow_factory
        self._sender = sender
        self._max_retries = max_retries

    def list_dispatchable(
        self,
        *,
        limit: int = 100,
        writeback_ids: Sequence[UUID] | None = None,
    ) -> tuple[RefundWritebackDispatchItem, ...]:
        """List candidates; the later claim remains the concurrency authority."""

        if limit <= 0:
            raise ValueError("limit must be positive")
        normalized_ids = tuple(dict.fromkeys(writeback_ids or ()))
        if writeback_ids is not None and not normalized_ids:
            return ()
        statement = (
            select(
                IncomeTaxRefundWriteback.id,
                IncomeTaxRefundWriteback.company_id,
                IncomeTaxRefundTarget.latest_scan_period,
            )
            .join(
                IncomeTaxRefundTarget,
                IncomeTaxRefundTarget.id == IncomeTaxRefundWriteback.target_id,
            )
            .where(
                _is_dispatchable(self._max_retries),
                IncomeTaxRefundTarget.latest_scan_period.is_not(None),
            )
            .order_by(
                IncomeTaxRefundWriteback.created_at,
                IncomeTaxRefundWriteback.id,
            )
            .limit(limit)
        )
        if normalized_ids:
            statement = statement.where(IncomeTaxRefundWriteback.id.in_(normalized_ids))
        with self._uow_factory() as uow:
            rows = tuple(uow.session.execute(statement).all())
        return tuple(
            RefundWritebackDispatchItem(
                writeback_id=writeback_id,
                company_id=company_id,
                scope_period=scope_period,
            )
            for writeback_id, company_id, scope_period in rows
            if scope_period is not None
        )

    def list_dispatchable_ids(
        self,
        *,
        limit: int = 100,
        writeback_ids: Sequence[UUID] | None = None,
    ) -> tuple[UUID, ...]:
        return tuple(
            item.writeback_id
            for item in self.list_dispatchable(
                limit=limit,
                writeback_ids=writeback_ids,
            )
        )

    def deliver(
        self,
        writeback_id: UUID,
        *,
        expected_company_id: UUID | None = None,
    ) -> RefundWritebackDelivery:
        claim, unclaimed = self._claim(
            writeback_id,
            expected_company_id=expected_company_id,
        )
        if claim is None:
            return unclaimed

        try:
            if isinstance(self._sender, RefundWritebackSchemaPreflight):
                self._sender.ensure_schema()
            self._sender.write_status(claim.company_code, claim.desired_value)
        except Exception as error:
            stored_error = _bounded_delivery_error(error)
            error_retryable = getattr(error, "retryable", True) is not False
            retry_after_seconds = _safe_retry_after_seconds(error)
            state_updated = self._finish(
                claim,
                status="FAILED",
                last_error=stored_error,
                processed_at=None,
            )
            return RefundWritebackDelivery(
                writeback_id=claim.writeback_id,
                company_id=claim.company_id,
                company_code=claim.company_code,
                status="FAILED" if state_updated else "PROCESSING",
                attempt_count=claim.attempt_count,
                claimed=True,
                retryable=(
                    state_updated and error_retryable and claim.attempt_count <= self._max_retries
                ),
                error_code=(stored_error if state_updated else "WRITEBACK_STATE_CHANGED"),
                retry_after_seconds=(retry_after_seconds if state_updated else None),
            )

        state_updated = self._finish(
            claim,
            status="SUCCEEDED",
            last_error=None,
            processed_at=datetime.now(timezone.utc),
        )
        return RefundWritebackDelivery(
            writeback_id=claim.writeback_id,
            company_id=claim.company_id,
            company_code=claim.company_code,
            status="SUCCEEDED" if state_updated else "PROCESSING",
            attempt_count=claim.attempt_count,
            claimed=True,
            retryable=False,
            error_code=None if state_updated else "WRITEBACK_STATE_CHANGED",
        )

    def _claim(
        self,
        writeback_id: UUID,
        *,
        expected_company_id: UUID | None,
    ) -> tuple[_Claim | None, RefundWritebackDelivery]:
        statement = (
            select(IncomeTaxRefundWriteback, Company.company_code)
            .join(Company, Company.id == IncomeTaxRefundWriteback.company_id)
            .where(
                IncomeTaxRefundWriteback.id == writeback_id,
                _is_claimable(self._max_retries),
            )
            .with_for_update(of=IncomeTaxRefundWriteback, skip_locked=True)
        )
        if expected_company_id is not None:
            statement = statement.where(IncomeTaxRefundWriteback.company_id == expected_company_id)
        with self._uow_factory() as uow:
            row = uow.session.execute(statement).one_or_none()
            if row is None:
                observed = uow.session.get(IncomeTaxRefundWriteback, writeback_id)
                if observed is None or (
                    expected_company_id is not None and observed.company_id != expected_company_id
                ):
                    return None, RefundWritebackDelivery(
                        writeback_id=writeback_id,
                        company_id=None,
                        company_code=None,
                        status="NOT_FOUND",
                        attempt_count=0,
                        claimed=False,
                        retryable=False,
                        error_code="WRITEBACK_NOT_FOUND",
                    )
                return None, RefundWritebackDelivery(
                    writeback_id=writeback_id,
                    company_id=observed.company_id,
                    company_code=None,
                    status=observed.status,
                    attempt_count=observed.attempt_count,
                    claimed=False,
                    retryable=False,
                    error_code=None,
                )

            writeback, company_code = row
            writeback.status = "PROCESSING"
            writeback.attempt_count += 1
            writeback.last_error = None
            writeback.processed_at = None
            claim = _Claim(
                writeback_id=writeback.id,
                company_id=writeback.company_id,
                company_code=company_code,
                desired_value=writeback.desired_value,
                attempt_count=writeback.attempt_count,
            )
            uow.commit()
        return claim, RefundWritebackDelivery(
            writeback_id=claim.writeback_id,
            company_id=claim.company_id,
            company_code=claim.company_code,
            status="PROCESSING",
            attempt_count=claim.attempt_count,
            claimed=True,
            retryable=False,
        )

    def _finish(
        self,
        claim: _Claim,
        *,
        status: str,
        last_error: str | None,
        processed_at: datetime | None,
    ) -> bool:
        statement = (
            select(IncomeTaxRefundWriteback)
            .where(
                IncomeTaxRefundWriteback.id == claim.writeback_id,
                IncomeTaxRefundWriteback.company_id == claim.company_id,
                IncomeTaxRefundWriteback.status == "PROCESSING",
                IncomeTaxRefundWriteback.attempt_count == claim.attempt_count,
            )
            .with_for_update()
        )
        with self._uow_factory() as uow:
            writeback = uow.session.execute(statement).scalar_one_or_none()
            if writeback is None:
                return False
            writeback.status = status
            writeback.last_error = last_error
            writeback.processed_at = processed_at
            uow.commit()
        return True


def _is_dispatchable(max_retries: int) -> ColumnElement[bool]:
    return and_(
        IncomeTaxRefundWriteback.attempt_count <= max_retries,
        or_(
            IncomeTaxRefundWriteback.status == "PENDING",
            IncomeTaxRefundWriteback.status == "FAILED",
        ),
    )


def _is_claimable(max_retries: int) -> ColumnElement[bool]:
    return or_(
        and_(
            IncomeTaxRefundWriteback.attempt_count <= max_retries,
            or_(
                IncomeTaxRefundWriteback.status == "PENDING",
                IncomeTaxRefundWriteback.status == "FAILED",
            ),
        ),
        # A worker may die after committing PROCESSING. Broker redelivery must be
        # able to resume that idempotent external write even after retry exhaustion.
        IncomeTaxRefundWriteback.status == "PROCESSING",
    )


def _bounded_delivery_error(error: Exception) -> str:
    # Exception messages can contain request headers or credentials. Persist only a type label.
    declared_code = getattr(error, "error_code", None)
    if (
        isinstance(declared_code, str)
        and 1 <= len(declared_code) <= 128
        and declared_code[0].isalpha()
        and all(
            character.isupper() or character.isdigit() or character == "_"
            for character in declared_code
        )
    ):
        return declared_code
    type_name = "".join(
        character if character.isalnum() or character == "_" else "_"
        for character in type(error).__name__
    )[:96]
    return f"REFUND_WRITEBACK_DELIVERY_FAILED:{type_name or 'Exception'}"[:192]


def _safe_retry_after_seconds(error: Exception) -> int | None:
    value = getattr(error, "retry_after_seconds", None)
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not isfinite(value)
        or value < 0
    ):
        return None
    return min(max(1, ceil(value)), 3_600)


__all__ = [
    "IncomeTaxRefundWritebackService",
    "RefundWritebackDelivery",
    "RefundWritebackDispatchItem",
    "RefundWritebackSchemaPreflight",
    "RefundWritebackSender",
]
