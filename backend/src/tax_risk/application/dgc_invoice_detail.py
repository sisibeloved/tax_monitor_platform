"""Request contract for the DGC invoice-detail source."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, TypeAlias

from tax_risk.adapters.ingest.dgc_sap_profit import DgcFetchResult


DgcInvoiceParameterValue: TypeAlias = str | int | float | bool | None


class DgcInvoiceDetailSource(Protocol):
    def fetch(
        self,
        parameters: Mapping[str, DgcInvoiceParameterValue],
    ) -> DgcFetchResult: ...


@dataclass(frozen=True, slots=True)
class DgcInvoiceDetailQuery:
    accounting_date: str | None = None
    comp: str | None = None


class DgcInvoiceDetailQueryService:
    """Fetch raw invoice rows while the response-field contract is pending."""

    def __init__(self, source: DgcInvoiceDetailSource) -> None:
        self._source = source

    def query(self, request: DgcInvoiceDetailQuery) -> DgcFetchResult:
        parameters: dict[str, DgcInvoiceParameterValue] = {}
        accounting_date = _optional_nonempty_text(
            request.accounting_date,
            "accounting_date",
        )
        company = _optional_nonempty_text(request.comp, "comp")
        if accounting_date is not None:
            parameters["accounting_date"] = accounting_date
        if company is not None:
            parameters["comp"] = company
        return self._source.fetch(parameters)


def _optional_nonempty_text(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"{field} must be a string or None")
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field} must be nonempty when provided")
    return normalized


__all__ = [
    "DgcInvoiceDetailQuery",
    "DgcInvoiceDetailQueryService",
    "DgcInvoiceDetailSource",
]
