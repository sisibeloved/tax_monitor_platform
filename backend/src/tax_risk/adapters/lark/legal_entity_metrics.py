"""Controlled field mapping for the legal-entity metrics Lark Base table."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
import math
from typing import Final

from tax_risk.adapters.ingest.base import fits_database_amount
from tax_risk.adapters.ingest.tax_master_xlsx import TaxMasterRow
from tax_risk.domain.money import Rate


LARK_LEGAL_ENTITY_METRICS_TABLE_ID: Final[str] = "tbl4PCNdcl4BYzgZ"


@dataclass(frozen=True, slots=True)
class LarkLegalEntityMetricFieldMap:
    company_code: str = "fld5uBjB9R"
    company_name: str = "fld65JDObx"
    tax_rate: str = "fldgeRGkKv"
    deferred_tax_rate: str = "fld3zvDri3"
    loss_carryforward: str = "fld70tcRFh"
    three_year_average_tax_burden: str = "fld5c2IX6N"
    refund_involved_2025: str = "fld6bBYJeP"
    refund_amount_2025: str = "fld5KnsfqZ"
    refund_status: str = "fld4HLnqDk"


@dataclass(frozen=True, slots=True)
class LarkLegalEntityMetricParseResult:
    rows: tuple[TaxMasterRow, ...]
    source_record_count: int
    excluded_blank_company_count: int


class LarkLegalEntityMetricError(ValueError):
    def __init__(
        self,
        error_code: str,
        message: str,
        *,
        row_number: int | None = None,
        field: str | None = None,
    ) -> None:
        self.error_code = error_code
        self.message = message
        self.row_number = row_number
        self.field = field
        super().__init__(message)


class LarkLegalEntityMetricsAdapter:
    """Convert projected Base records into point-in-time tax master rows."""

    def __init__(
        self,
        records: tuple[Mapping[str, object], ...],
        *,
        valid_from: date,
        valid_to: date | None = None,
        field_map: LarkLegalEntityMetricFieldMap = LarkLegalEntityMetricFieldMap(),
    ) -> None:
        if type(records) is not tuple or any(not isinstance(item, Mapping) for item in records):
            raise TypeError("records must be an immutable tuple of mappings")
        if type(valid_from) is not date:
            raise TypeError("valid_from must be a date")
        if valid_to is not None and type(valid_to) is not date:
            raise TypeError("valid_to must be a date or None")
        if valid_to is not None and valid_to < valid_from:
            raise ValueError("valid_to must be on or after valid_from")
        if not isinstance(field_map, LarkLegalEntityMetricFieldMap):
            raise TypeError("field_map must be a LarkLegalEntityMetricFieldMap")
        self._records = records
        self._valid_from = valid_from
        self._valid_to = valid_to
        self._field_map = field_map

    def parse(self) -> LarkLegalEntityMetricParseResult:
        parsed: list[TaxMasterRow] = []
        excluded = 0
        seen_companies: set[str] = set()
        for row_number, record in enumerate(self._records, start=1):
            company_code = _text_cell(record.get(self._field_map.company_code))
            if company_code is None:
                excluded += 1
                continue
            if company_code in seen_companies:
                raise LarkLegalEntityMetricError(
                    "DUPLICATE_COMPANY_CODE",
                    "Lark Base contains more than one nonblank record for a company code",
                    row_number=row_number,
                    field="公司代码",
                )
            seen_companies.add(company_code)
            company_name = _required_text_cell(
                record.get(self._field_map.company_name),
                row_number,
                "公司名称",
            )
            tax_rate = _rate(
                record.get(self._field_map.tax_rate),
                row_number,
                "所得税税率",
            )
            deferred_tax_rate = _rate(
                record.get(self._field_map.deferred_tax_rate),
                row_number,
                "递延所得税税率",
            )
            loss = _decimal(
                record.get(self._field_map.loss_carryforward),
                row_number,
                "可弥补亏损额合计",
            )
            if loss < 0:
                raise LarkLegalEntityMetricError(
                    "INVALID_LOSS_CARRYFORWARD",
                    "可弥补亏损额合计 must not be negative",
                    row_number=row_number,
                    field="可弥补亏损额合计",
                )
            burden = _rate(
                record.get(self._field_map.three_year_average_tax_burden),
                row_number,
                "3年平均税负率",
            )
            parsed.append(
                TaxMasterRow(
                    row_number=row_number,
                    company_code=company_code,
                    company_name=company_name,
                    valid_from=self._valid_from,
                    valid_to=self._valid_to,
                    tax_rate=tax_rate,
                    deferred_tax_rate=deferred_tax_rate,
                    loss_carryforward=loss,
                    three_year_average_tax_burden=burden,
                )
            )
        return LarkLegalEntityMetricParseResult(
            rows=tuple(parsed),
            source_record_count=len(self._records),
            excluded_blank_company_count=excluded,
        )


def _text_cell(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, Mapping):
        text = value.get("text")
        return text.strip() or None if isinstance(text, str) else None
    if isinstance(value, list):
        parts: list[str] = []
        for part in value:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, Mapping) and isinstance(part.get("text"), str):
                parts.append(part["text"])
            else:
                return None
        normalized = "".join(parts).strip()
        return normalized or None
    return None


def _required_text_cell(value: object, row_number: int, field: str) -> str:
    parsed = _text_cell(value)
    if parsed is None:
        raise LarkLegalEntityMetricError(
            "MISSING_VALUE",
            f"{field} is required for a nonblank company code",
            row_number=row_number,
            field=field,
        )
    return parsed


def _rate(value: object, row_number: int, field: str) -> Rate:
    parsed = _decimal(value, row_number, field)
    try:
        return Rate.from_fraction(parsed)
    except (TypeError, ValueError) as error:
        raise LarkLegalEntityMetricError(
            "INVALID_RATE",
            f"{field} must use the Base fractional value from 0 through 1",
            row_number=row_number,
            field=field,
        ) from error


def _decimal(value: object, row_number: int, field: str) -> Decimal:
    try:
        if isinstance(value, bool) or value is None:
            raise InvalidOperation
        if isinstance(value, Decimal):
            parsed = value
        elif type(value) is int:
            parsed = Decimal(value)
        elif type(value) is float and math.isfinite(value):
            parsed = Decimal(str(value))
        elif isinstance(value, str) and value.strip():
            parsed = Decimal(value.strip().replace(",", ""))
        else:
            raise InvalidOperation
    except (InvalidOperation, ValueError) as error:
        raise LarkLegalEntityMetricError(
            "INVALID_NUMBER",
            f"{field} must be a finite decimal number",
            row_number=row_number,
            field=field,
        ) from error
    if not parsed.is_finite() or not fits_database_amount(parsed):
        raise LarkLegalEntityMetricError(
            "INVALID_NUMBER",
            f"{field} must be finite and fit NUMERIC(38,12)",
            row_number=row_number,
            field=field,
        )
    return parsed


__all__ = [
    "LARK_LEGAL_ENTITY_METRICS_TABLE_ID",
    "LarkLegalEntityMetricError",
    "LarkLegalEntityMetricFieldMap",
    "LarkLegalEntityMetricParseResult",
    "LarkLegalEntityMetricsAdapter",
]
