from __future__ import annotations

from collections.abc import Iterator, Mapping
import csv
from hashlib import sha256
import io
from typing import ClassVar, cast

from pydantic import BaseModel, ValidationError

from tax_risk.adapters.ingest.base import AdapterRow, CanonicalRow, RowError
from tax_risk.adapters.ingest.csv_adapter import HeaderValidationError


class BusinessEntertainmentCsvAdapter:
    """Strict UTF-8 CSV adapter shared by the five governed evidence sources."""

    HEADER: ClassVar[tuple[str, ...]]
    DATASET_CODE: ClassVar[str]
    SCHEMA_VERSION: ClassVar[str]
    PRIMARY_KEY_FIELDS: ClassVar[tuple[str, ...]]
    RECORD_TYPE: ClassVar[type[BaseModel]]

    def __init__(self, payload: bytes) -> None:
        if not isinstance(payload, bytes):
            raise TypeError("CSV payload must be bytes")
        self._payload = payload
        self._text: str | None = None

    @property
    def checksum(self) -> str:
        return sha256(self._payload).hexdigest()

    @property
    def dataset_code(self) -> str:
        return self.DATASET_CODE

    @property
    def schema_version(self) -> str:
        return self.SCHEMA_VERSION

    @property
    def source_primary_key_definition(self) -> dict[str, list[str]]:
        return {"fields": list(self.PRIMARY_KEY_FIELDS)}

    def validate_header(self) -> None:
        reader = csv.DictReader(io.StringIO(self._read_text(), newline=""))
        actual = tuple(reader.fieldnames or ())
        missing = tuple(column for column in self.HEADER if column not in actual)
        extra = tuple(column for column in actual if column not in self.HEADER)
        if missing or extra or len(actual) != len(set(actual)):
            raise HeaderValidationError(
                "INVALID_HEADER",
                f"invalid header for {self.DATASET_CODE}",
                missing_columns=missing,
                extra_columns=extra,
            )

    def iter_rows(self) -> Iterator[AdapterRow]:
        self.validate_header()
        reader = csv.DictReader(io.StringIO(self._read_text(), newline=""))
        seen: set[str] = set()
        for raw in reader:
            row_number = reader.line_num
            if None in raw or any(value is None for value in raw.values()):
                yield _error(row_number, "ROW_COLUMN_COUNT_MISMATCH", "row width is invalid")
                continue
            try:
                record = self.RECORD_TYPE.model_validate(_normalize(raw))
            except ValidationError as error:
                yield _validation_error(row_number, error)
                continue
            source_key = cast(str, getattr(record, "source_record_key"))
            if source_key in seen:
                yield _error(
                    row_number,
                    "DUPLICATE_SOURCE_RECORD_KEY",
                    "source record key is duplicated within the file",
                    field="source_record_key",
                    rejected_value=source_key,
                )
                continue
            seen.add(source_key)
            yield AdapterRow(
                row_number=row_number,
                value=cast(CanonicalRow, record),
                error=None,
            )

    def _read_text(self) -> str:
        if self._text is not None:
            return self._text
        try:
            self._text = self._payload.decode("utf-8-sig")
        except UnicodeDecodeError as error:
            raise HeaderValidationError(
                "INVALID_ENCODING",
                "CSV file must be UTF-8 encoded",
            ) from error
        return self._text


def _normalize(raw: Mapping[str | None, str | list[str] | None]) -> dict[str, object]:
    return {
        str(key): (None if isinstance(value, str) and not value.strip() else value)
        for key, value in raw.items()
        if key is not None
    }


def _validation_error(row_number: int, error: ValidationError) -> AdapterRow:
    issue = error.errors(include_url=False)[0]
    field = str(issue["loc"][0]) if issue["loc"] else None
    issue_type = str(issue["type"])
    if issue_type in {"decimal_parsing", "decimal_type"}:
        error_code = "INVALID_DECIMAL"
    elif issue_type in {"missing", "string_too_short"}:
        error_code = "MISSING_VALUE"
    else:
        error_code = "INVALID_VALUE"
    rejected = issue.get("input")
    return _error(
        row_number,
        error_code,
        str(issue["msg"]),
        field=field,
        rejected_value=None if rejected is None else str(rejected),
    )


def _error(
    row_number: int,
    error_code: str,
    message: str,
    *,
    field: str | None = None,
    rejected_value: str | None = None,
) -> AdapterRow:
    return AdapterRow(
        row_number=row_number,
        value=None,
        error=RowError(
            row_number=row_number,
            error_code=error_code,
            message=message,
            field=field,
            rejected_value=rejected_value,
        ),
    )


__all__ = ["BusinessEntertainmentCsvAdapter"]
