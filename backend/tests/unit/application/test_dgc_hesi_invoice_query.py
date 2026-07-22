from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import pytest

from tax_risk.adapters.ingest.dgc_sap_profit import DgcFetchResult
from tax_risk.application.dgc_hesi_invoice import (
    DgcHesiInvoiceQuery,
    DgcHesiInvoiceQueryService,
)


@dataclass
class _Source:
    calls: list[dict[str, object]] = field(default_factory=list)

    def fetch(self, parameters: Mapping[str, object]) -> DgcFetchResult:
        self.calls.append(dict(parameters))
        return DgcFetchResult(records=({"hesi_invoice": "raw"},), checksum="a" * 64)


def test_query_sends_only_supplied_normalized_company_filter() -> None:
    source = _Source()

    result = DgcHesiInvoiceQueryService(source).query(
        DgcHesiInvoiceQuery(company_code=" 3000 ")
    )

    assert source.calls == [{"company_code": "3000"}]
    assert result.records == ({"hesi_invoice": "raw"},)


def test_query_allows_unfiltered_read_and_leaves_pagination_to_client() -> None:
    source = _Source()

    DgcHesiInvoiceQueryService(source).query(DgcHesiInvoiceQuery())

    assert source.calls == [{}]
    assert "limitValue" not in source.calls[0]
    assert "offsetValue" not in source.calls[0]


def test_query_rejects_blank_optional_company_filter() -> None:
    source = _Source()

    with pytest.raises(ValueError, match="company_code"):
        DgcHesiInvoiceQueryService(source).query(DgcHesiInvoiceQuery(company_code=" "))

    assert source.calls == []
