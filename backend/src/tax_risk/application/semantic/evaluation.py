"""Governed golden-set contracts and release metrics for semantic monitoring."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class IndependentReview(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    role: Literal["FINANCE", "TAX"]
    reviewer_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    risk: bool
    reviewed_at: datetime


class Adjudication(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    adjudicator_id: str = Field(min_length=1)
    label: str = Field(min_length=1)
    risk: bool
    adjudicated_at: datetime


class GoldFileManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    path: Literal["welfare.jsonl", "donation.jsonl"]
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    row_count: int = Field(ge=50)
    gold_set_version: str = Field(min_length=1)
    status: Literal["APPROVED"]
    frozen: Literal[True]
    approved_by: str = Field(min_length=1)
    approved_at: datetime


class GoldManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    status: Literal["APPROVED"]
    frozen: Literal[True]
    approved_by: str = Field(min_length=1)
    approved_at: datetime
    files: tuple[GoldFileManifest, GoldFileManifest]

    @model_validator(mode="after")
    def validate_file_members(self) -> GoldManifest:
        if {entry.path for entry in self.files} != {"welfare.jsonl", "donation.jsonl"}:
            raise ValueError("manifest must freeze welfare.jsonl and donation.jsonl")
        return self


class GoldRow(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    id: str = Field(min_length=1)
    subject: Literal["WELFARE", "DONATION"]
    company_code: str = Field(min_length=1)
    period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    sap_fiscal_year: int = Field(ge=2000, le=9999)
    voucher_no: str = Field(min_length=1)
    line_item_no: str = Field(min_length=1)
    current_account: str = Field(min_length=1)
    amount: Decimal
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    summary: str = Field(min_length=1, max_length=2000)
    case_tags: tuple[str, ...]
    expected_label: str = Field(min_length=1)
    expected_risk: bool
    finance_review: IndependentReview
    tax_review: IndependentReview
    adjudication: Adjudication
    gold_set_version: str = Field(min_length=1)
    approval_status: Literal["APPROVED"]
    approved_by: str = Field(min_length=1)
    approved_at: datetime
    frozen: Literal[True]
    frozen_at: datetime
    row_checksum: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_review_chain(self) -> GoldRow:
        if self.finance_review.role != "FINANCE" or self.tax_review.role != "TAX":
            raise ValueError("finance and tax review roles are fixed")
        reviewers = {
            self.finance_review.reviewer_id,
            self.tax_review.reviewer_id,
            self.adjudication.adjudicator_id,
        }
        if len(reviewers) != 3:
            raise ValueError("reviewers and adjudicator must be independent")
        if (self.expected_label, self.expected_risk) != (
            self.adjudication.label,
            self.adjudication.risk,
        ):
            raise ValueError("expected result must equal adjudication")
        if not self.case_tags or len(self.case_tags) != len(set(self.case_tags)):
            raise ValueError("case tags must be non-empty and unique")
        if self.approved_at.tzinfo is None or self.frozen_at.tzinfo is None:
            raise ValueError("approval and freeze timestamps must be timezone-aware")
        return self


class EvaluatedRow(GoldRow):
    predicted_label: str
    predicted_risk: bool
    confidence: Literal["HIGH", "MEDIUM", "LOW"]


@dataclass(frozen=True, slots=True)
class EvaluationMetrics:
    recall: float
    high_confidence_accuracy: float
    expected_positive_count: int
    true_positive_count: int
    high_confidence_risk_prediction_count: int
    high_confidence_correct_count: int


@dataclass(frozen=True, slots=True)
class EvaluationGate:
    minimum_recall: float
    minimum_high_confidence_accuracy: float

    def accepts(self, metrics: EvaluationMetrics) -> bool:
        return (
            metrics.expected_positive_count > 0
            and metrics.high_confidence_risk_prediction_count > 0
            and metrics.recall >= self.minimum_recall
            and metrics.high_confidence_accuracy >= self.minimum_high_confidence_accuracy
        )


PILOT_GATE = EvaluationGate(0.90, 0.80)
PRODUCTION_GATE = EvaluationGate(0.95, 0.80)


def evaluate(rows: list[EvaluatedRow]) -> EvaluationMetrics:
    positives = [row for row in rows if row.expected_risk]
    true_positives = [row for row in positives if row.predicted_risk]
    high_risk_predictions = [row for row in rows if row.confidence == "HIGH" and row.predicted_risk]
    high_correct = [
        row
        for row in high_risk_predictions
        if row.expected_risk and row.predicted_label == row.expected_label
    ]
    recall = len(true_positives) / len(positives) if positives else 0.0
    accuracy = len(high_correct) / len(high_risk_predictions) if high_risk_predictions else 0.0
    return EvaluationMetrics(
        recall=recall,
        high_confidence_accuracy=accuracy,
        expected_positive_count=len(positives),
        true_positive_count=len(true_positives),
        high_confidence_risk_prediction_count=len(high_risk_predictions),
        high_confidence_correct_count=len(high_correct),
    )


def canonical_row_sha256(row: GoldRow) -> str:
    payload = row.model_dump(mode="json", exclude={"row_checksum"})
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_gold_rows(path: Path) -> list[GoldRow]:
    return [
        GoldRow.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


__all__ = [
    "Adjudication",
    "EvaluatedRow",
    "EvaluationGate",
    "EvaluationMetrics",
    "GoldFileManifest",
    "GoldManifest",
    "GoldRow",
    "IndependentReview",
    "PILOT_GATE",
    "PRODUCTION_GATE",
    "canonical_row_sha256",
    "evaluate",
    "load_gold_rows",
    "sha256_file",
]
