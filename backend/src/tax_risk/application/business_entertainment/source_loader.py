"""Load immutable business-entertainment inputs from a published snapshot set."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from uuid import UUID

from tax_risk.domain.semantic.sap_voucher import (
    AccountFamily,
    SnapshotBoundSapExpenseVoucher,
)
from tax_risk.persistence.repositories import UnitOfWork
from tax_risk.persistence.semantic_repositories import SnapshotBoundSourceError


@dataclass(frozen=True, slots=True)
class SourceLoadIssue:
    error_code: str
    field: str
    remediation: str


@dataclass(frozen=True, slots=True)
class SapVoucherLoadResult:
    records: tuple[SnapshotBoundSapExpenseVoucher, ...]
    issues: tuple[SourceLoadIssue, ...]


class EntertainmentSnapshotSourceLoader:
    def __init__(self, uow_factory: Callable[[], UnitOfWork]) -> None:
        self._uow_factory = uow_factory

    def load_sap_vouchers(
        self,
        *,
        snapshot_set_id: UUID,
        company_code: str,
        period_end: date,
    ) -> SapVoucherLoadResult:
        try:
            with self._uow_factory() as uow:
                records = uow.semantic.load_snapshot_bound_sap_vouchers(
                    snapshot_set_id,
                    AccountFamily.BUSINESS_ENTERTAINMENT,
                    company_code.strip(),
                    period_end,
                )
                return SapVoucherLoadResult(tuple(records), ())
        except SnapshotBoundSourceError as error:
            return SapVoucherLoadResult(
                (),
                (
                    SourceLoadIssue(
                        error_code=error.error_code,
                        field="snapshot_set_id",
                        remediation=str(error),
                    ),
                ),
            )


__all__ = [
    "EntertainmentSnapshotSourceLoader",
    "SapVoucherLoadResult",
    "SourceLoadIssue",
]
