"""双人复核、裁决、审批和冻结的业务招待费黄金数据契约。"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from hashlib import sha256
import json
from pathlib import Path
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_IDENTITY = re.compile(r"(?<!\d)\d{17}[\dXx](?![\dXx])")
_SEMANTIC_SOURCE_MODES = frozenset({"SAP_LINKED", "BUSINESS_DOCUMENT_UNLINKED"})


class GoldenDatasetError(ValueError):
    pass


class GoldenAnnotation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    label: str = Field(min_length=1, max_length=64)
    annotator_id: str = Field(min_length=1, max_length=128)
    annotated_at: datetime

    @field_validator("annotated_at")
    @classmethod
    def annotation_time_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("annotation time must be timezone-aware")
        return value


class GoldenRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)

    sample_id: str = Field(pattern=r"^BE-G-\d{3}$")
    frozen_version: str = Field(min_length=1, max_length=128)
    redacted_input: dict[str, str]
    source_mode: str = Field(
        pattern=r"^(SAP_LINKED|BUSINESS_DOCUMENT_UNLINKED|SAP_COVERAGE_ONLY)$"
    )
    expected_evidence: tuple[str, ...]
    finance_annotation: GoldenAnnotation
    tax_annotation: GoldenAnnotation
    adjudicator_id: str = Field(min_length=1, max_length=128)
    final_label: str = Field(min_length=1, max_length=64)
    expected_alert: bool
    candidate_hit: bool
    model_label: str = Field(min_length=1, max_length=64)
    reviewed_label: str = Field(min_length=1, max_length=64)
    confidence_tier: str = Field(pattern=r"^(HIGH|MEDIUM|LOW)$")
    known_case: bool
    approval_status: str
    freeze_status: str
    frozen_at: datetime
    record_checksum: str = Field(min_length=64, max_length=64)
    frozen_version_checksum: str = Field(min_length=64, max_length=64)

    @field_validator("frozen_at")
    @classmethod
    def frozen_time_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("frozen time must be timezone-aware")
        return value

    @model_validator(mode="after")
    def enforce_dual_review_and_release_state(self) -> GoldenRecord:
        reviewers = {
            self.finance_annotation.annotator_id,
            self.tax_annotation.annotator_id,
            self.adjudicator_id,
        }
        if len(reviewers) != 3:
            raise ValueError("finance, tax, and adjudication reviewers must be distinct")
        if self.approval_status != "APPROVED" or self.freeze_status != "FROZEN":
            raise ValueError("only APPROVED and FROZEN records are release eligible")
        serialized = json.dumps(self.redacted_input, ensure_ascii=False, sort_keys=True)
        if _PHONE.search(serialized) or _IDENTITY.search(serialized):
            raise ValueError("redacted input still contains direct PII")
        return self

    @property
    def is_semantic_sample(self) -> bool:
        return self.source_mode in _SEMANTIC_SOURCE_MODES


class GoldenDataset(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: str
    frozen_version_checksum: str
    records: tuple[GoldenRecord, ...]


def load_golden_dataset(path: Path) -> GoldenDataset:
    records: list[dict[str, object]] = []
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as error:
            raise GoldenDatasetError(f"invalid JSONL at line {line_number}") from error
        if not isinstance(payload, dict):
            raise GoldenDatasetError(f"golden line {line_number} must be an object")
        records.append(payload)
    return validate_golden_records(records)


def validate_golden_records(
    records: Sequence[Mapping[str, object]],
) -> GoldenDataset:
    if not records:
        raise GoldenDatasetError("golden dataset must not be empty")
    try:
        parsed = tuple(GoldenRecord.model_validate(record) for record in records)
    except ValueError as error:
        raise GoldenDatasetError(str(error)) from error
    sample_ids = [record.sample_id for record in parsed]
    if len(sample_ids) != len(set(sample_ids)):
        raise GoldenDatasetError("golden sample ids must be unique")
    versions = {record.frozen_version for record in parsed}
    if len(versions) != 1:
        raise GoldenDatasetError("all golden records must share one frozen version")
    for record in parsed:
        expected = record_checksum(record.model_dump(mode="json"))
        if record.record_checksum != expected:
            raise GoldenDatasetError(f"record checksum mismatch for {record.sample_id}")
    expected_dataset_checksum = dataset_checksum(parsed)
    declared_checksums = {record.frozen_version_checksum for record in parsed}
    if declared_checksums != {expected_dataset_checksum}:
        raise GoldenDatasetError("frozen version checksum mismatch")
    return GoldenDataset(
        version=next(iter(versions)),
        frozen_version_checksum=expected_dataset_checksum,
        records=parsed,
    )


def record_checksum(record: Mapping[str, object]) -> str:
    payload = {
        key: value
        for key, value in record.items()
        if key not in {"record_checksum", "frozen_version_checksum"}
    }
    return _checksum(payload)


def dataset_checksum(records: Sequence[GoldenRecord]) -> str:
    identities = [
        {"sample_id": record.sample_id, "record_checksum": record.record_checksum}
        for record in sorted(records, key=lambda item: item.sample_id)
    ]
    return _checksum(identities)


def _checksum(value: object) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()


__all__ = [
    "GoldenAnnotation",
    "GoldenDataset",
    "GoldenDatasetError",
    "GoldenRecord",
    "dataset_checksum",
    "load_golden_dataset",
    "record_checksum",
    "validate_golden_records",
]
