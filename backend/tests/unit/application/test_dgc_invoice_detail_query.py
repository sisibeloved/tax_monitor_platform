from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import pytest

from tax_risk.adapters.ingest.dgc_sap_profit import DgcFetchResult
from tax_risk.application.dgc_invoice_detail import (
    DgcInvoiceDetailQuery,
    DgcInvoiceDetailQueryService,
)


@dataclass
class _Source:
    calls: list[dict[str, object]] = field(default_factory=list)

    def fetch(self, parameters: Mapping[str, object]) -> DgcFetchResult:
        self.calls.append(dict(parameters))
        return DgcFetchResult(records=({"invoice": "raw"},), checksum="a" * 64)


def test_query_sends_only_supplied_normalized_filters() -> None:
    source = _Source()

    result = DgcInvoiceDetailQueryService(source).query(
        DgcInvoiceDetailQuery(
            accounting_date=" 2026-07-01 ",
            comp=" 3000 ",
        )
    )

    assert source.calls == [{"accounting_date": "2026-07-01", "comp": "3000"}]
    assert result.records == ({"invoice": "raw"},)


def test_query_allows_unfiltered_read_and_leaves_pagination_to_client() -> None:
    source = _Source()

    DgcInvoiceDetailQueryService(source).query(DgcInvoiceDetailQuery())

    assert source.calls == [{}]
    assert "limitValue" not in source.calls[0]
    assert "offsetValue" not in source.calls[0]


@pytest.mark.parametrize("field", ("accounting_date", "comp"))
def test_query_rejects_blank_optional_filters(field: str) -> None:
    source = _Source()

    with pytest.raises(ValueError, match=field):
        DgcInvoiceDetailQueryService(source).query(DgcInvoiceDetailQuery(**{field: " "}))

    assert source.calls == []
