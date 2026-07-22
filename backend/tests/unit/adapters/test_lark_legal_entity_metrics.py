from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from tax_risk.adapters.lark.legal_entity_metrics import (
    LARK_LEGAL_ENTITY_METRICS_TABLE_ID,
    LarkLegalEntityMetricError,
    LarkLegalEntityMetricFieldMap,
    LarkLegalEntityMetricsAdapter,
)


FIELDS = LarkLegalEntityMetricFieldMap()


def _record(company_code: object = "3000", **overrides: object) -> dict[str, object]:
    record: dict[str, object] = {
        FIELDS.company_code: company_code,
        FIELDS.company_name: "Company 3000",
        FIELDS.tax_rate: 0.25,
        FIELDS.deferred_tax_rate: 0,
        FIELDS.loss_carryforward: 29_439_008.19,
        FIELDS.three_year_average_tax_burden: 0,
    }
    record.update(overrides)
    return record


def test_default_mapping_matches_the_verified_lark_base_schema() -> None:
    assert LARK_LEGAL_ENTITY_METRICS_TABLE_ID == "tbl4PCNdcl4BYzgZ"
    assert FIELDS == LarkLegalEntityMetricFieldMap(
        company_code="fld5uBjB9R",
        company_name="fld65JDObx",
        tax_rate="fldgeRGkKv",
        deferred_tax_rate="fld3zvDri3",
        loss_carryforward="fld70tcRFh",
        three_year_average_tax_burden="fld5c2IX6N",
        refund_involved_2025="fld6bBYJeP",
        refund_amount_2025="fld5KnsfqZ",
        refund_status="fld4HLnqDk",
    )


def test_parser_uses_fractional_rates_and_excludes_blank_company_codes() -> None:
    result = LarkLegalEntityMetricsAdapter(
        (
            _record(),
            _record(None),
            _record("   "),
        ),
        valid_from=date(2026, 1, 1),
    ).parse()

    assert result.source_record_count == 3
    assert result.excluded_blank_company_count == 2
    assert len(result.rows) == 1
    row = result.rows[0]
    assert row.company_code == "3000"
    assert row.tax_rate.value == Decimal("0.25")
    assert row.deferred_tax_rate.value == Decimal("0")
    assert row.loss_carryforward == Decimal("29439008.19")
    assert row.three_year_average_tax_burden.value == Decimal("0")
    assert row.valid_from == date(2026, 1, 1)


def test_duplicate_nonblank_company_codes_are_rejected() -> None:
    with pytest.raises(LarkLegalEntityMetricError) as caught:
        LarkLegalEntityMetricsAdapter(
            (_record(), _record()),
            valid_from=date(2026, 1, 1),
        ).parse()

    assert caught.value.error_code == "DUPLICATE_COMPANY_CODE"
    assert caught.value.field == "公司代码"


@pytest.mark.parametrize(
    ("field_id", "field_name"),
    [
        (FIELDS.company_name, "公司名称"),
        (FIELDS.tax_rate, "所得税税率"),
        (FIELDS.deferred_tax_rate, "递延所得税税率"),
        (FIELDS.loss_carryforward, "可弥补亏损额合计"),
        (FIELDS.three_year_average_tax_burden, "3年平均税负率"),
    ],
)
def test_nonblank_company_requires_every_quarterly_master_field(
    field_id: str,
    field_name: str,
) -> None:
    with pytest.raises(LarkLegalEntityMetricError) as caught:
        LarkLegalEntityMetricsAdapter(
            (_record(**{field_id: None}),),
            valid_from=date(2026, 1, 1),
        ).parse()

    assert caught.value.field == field_name


def test_percentage_display_value_must_not_be_misread_as_fraction() -> None:
    with pytest.raises(LarkLegalEntityMetricError) as caught:
        LarkLegalEntityMetricsAdapter(
            (_record(**{FIELDS.tax_rate: 25}),),
            valid_from=date(2026, 1, 1),
        ).parse()

    assert caught.value.error_code == "INVALID_RATE"
    assert caught.value.field == "所得税税率"


def test_loss_carryforward_must_be_nonnegative() -> None:
    with pytest.raises(LarkLegalEntityMetricError) as caught:
        LarkLegalEntityMetricsAdapter(
            (_record(**{FIELDS.loss_carryforward: "-1"}),),
            valid_from=date(2026, 1, 1),
        ).parse()

    assert caught.value.error_code == "INVALID_LOSS_CARRYFORWARD"
