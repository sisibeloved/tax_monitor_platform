"""Request contract for the DGC Hesi reimbursement-detail source."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Protocol, TypeAlias

from tax_risk.adapters.ingest.dgc_sap_profit import DgcFetchResult


DgcHesiParameterValue: TypeAlias = str | int | float | bool | None


class DgcHesiReimbursementSource(Protocol):
    def fetch(
        self,
        parameters: Mapping[str, DgcHesiParameterValue],
    ) -> DgcFetchResult: ...


@dataclass(frozen=True, slots=True)
class DgcHesiReimbursementQuery:
    company_code: str | None = None
    submit_date: str | None = None


class DgcHesiReimbursementQueryService:
    """Fetch raw Hesi rows while the response-field contract is being validated."""

    def __init__(self, source: DgcHesiReimbursementSource) -> None:
        self._source = source

    def query(self, request: DgcHesiReimbursementQuery) -> DgcFetchResult:
        parameters: dict[str, DgcHesiParameterValue] = {}
        company_code = _optional_nonempty_text(request.company_code, "company_code")
        submit_date = _optional_nonempty_text(request.submit_date, "submit_date")
        if company_code is not None:
            parameters["company_code"] = company_code
        if submit_date is not None:
            parameters["submit_date"] = submit_date
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
    "DgcHesiReimbursementQuery",
    "DgcHesiReimbursementQueryService",
    "DgcHesiReimbursementSource",
]
