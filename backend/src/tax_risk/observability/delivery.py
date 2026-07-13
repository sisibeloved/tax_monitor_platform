"""Pure delivery-timestamp rules shared by quarterly and monthly orchestration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Iterable


_ACTIVE_STATUSES = frozenset({"PENDING", "RUNNING", "RETRY_PENDING"})


@dataclass(frozen=True, slots=True)
class BatchDelivery:
    batch_finished_at: datetime | None
    output_ready_at: datetime | None


def derive_batch_delivery(
    company_outputs: Iterable[tuple[str, datetime | None]],
    *,
    now: datetime,
) -> BatchDelivery:
    values = tuple(company_outputs)
    if not values or any(status in _ACTIVE_STATUSES for status, _ in values):
        return BatchDelivery(None, None)
    if all(status == "SUCCEEDED" and ready_at is not None for status, ready_at in values):
        return BatchDelivery(now, max(ready_at for _, ready_at in values if ready_at is not None))
    return BatchDelivery(now, None)


__all__ = ["BatchDelivery", "derive_batch_delivery"]
