"""Request contract for the DGC Hesi reimbursement-invoice source."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, TypeAlias

from tax_risk.adapters.ingest.dgc_sap_profit import DgcFetchResult


DgcHesiInvoiceParameterValue: TypeAlias = str | int | float | bool | None


class DgcHesiInvoiceSource(Protocol):
    def fetch(
        self,
        parameters: Mapping[str, DgcHesiInvoiceParameterValue],
    ) -> DgcFetchResult: ...


@dataclass(frozen=True, slots=True)
class DgcHesiInvoiceQuery:
    company_code: str | None = None


class DgcHesiInvoiceQueryService:
    """Fetch raw Hesi invoice rows while the response-field contract is pending."""

    def __init__(self, source: DgcHesiInvoiceSource) -> None:
        self._source = source

    def query(self, request: DgcHesiInvoiceQuery) -> DgcFetchResult:
        parameters: dict[str, DgcHesiInvoiceParameterValue] = {}
        company_code = _optional_nonempty_text(request.company_code, "company_code")
        if company_code is not None:
            parameters["company_code"] = company_code
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
    "DgcHesiInvoiceQuery",
    "DgcHesiInvoiceQueryService",
    "DgcHesiInvoiceSource",
]
