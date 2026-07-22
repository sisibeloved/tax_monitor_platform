from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

import pytest

from tax_risk.adapters.ingest.dgc_sap_profit import DgcFetchResult
from tax_risk.application.dgc_hesi_reimbursement import (
    DgcHesiReimbursementQuery,
    DgcHesiReimbursementQueryService,
)


@dataclass
class _Source:
    calls: list[dict[str, object]] = field(default_factory=list)

    def fetch(self, parameters: Mapping[str, object]) -> DgcFetchResult:
        self.calls.append(dict(parameters))
        return DgcFetchResult(records=({"hesi": "raw"},), checksum="a" * 64)


def test_query_sends_only_supplied_normalized_filters() -> None:
    source = _Source()

    result = DgcHesiReimbursementQueryService(source).query(
        DgcHesiReimbursementQuery(
            company_code=" 3000 ",
            submit_date=" 2026-07-01 ",
        )
    )

    assert source.calls == [{"company_code": "3000", "submit_date": "2026-07-01"}]
    assert result.records == ({"hesi": "raw"},)


def test_query_allows_unfiltered_read_and_leaves_pagination_to_client() -> None:
    source = _Source()

    DgcHesiReimbursementQueryService(source).query(DgcHesiReimbursementQuery())

    assert source.calls == [{}]
    assert "limitValue" not in source.calls[0]
    assert "offsetValue" not in source.calls[0]


@pytest.mark.parametrize("field", ("company_code", "submit_date"))
def test_query_rejects_blank_optional_filters(field: str) -> None:
    source = _Source()

    with pytest.raises(ValueError, match=field):
        DgcHesiReimbursementQueryService(source).query(DgcHesiReimbursementQuery(**{field: " "}))

    assert source.calls == []
