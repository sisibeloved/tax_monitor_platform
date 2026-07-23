"""Transactional application service for monthly income-tax refund scans."""

from __future__ import annotations

from calendar import monthrange
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
from uuid import UUID

from sqlalchemy import select

from tax_risk.domain.income_tax_refund import (
    IncomeTaxRefundCandidate,
    IncomeTaxRefundInputs,
    RefundAccountFamily,
    RefundBookingStatus,
    RefundReceiptStatus,
    RefundScanPeriod,
    evaluate_income_tax_refund,
)
from tax_risk.domain.cases import MonitorType
from tax_risk.domain.money import Money
from tax_risk.persistence.income_tax_refund_models import (
    IncomeTaxRefundScanResult,
    IncomeTaxRefundTarget,
    IncomeTaxRefundWriteback,
    SapGlLineObservation,
    SapRefundEvidenceBatch,
)
from tax_risk.persistence.ingest_models import Company
from tax_risk.persistence.repositories import UnitOfWork
from tax_risk.persistence.risk_models import RiskCase, RiskCaseStatus


UowFactory = Callable[[], UnitOfWork]


class IncomeTaxRefundServiceError(RuntimeError):
    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        super().__init__(message)


@dataclass(frozen=True, slots=True)
class IncomeTaxRefundTargetDraft:
    company_code: str
    source_record_key: str
    expected_refund_amount: Decimal
    raw_expected_refund_amount: Decimal | None
    currency: str
    amount_scale: int
    received_in_source: bool = False


@dataclass(frozen=True, slots=True)
class SapRefundLineDraft:
    company_code: str
    client: str
    ledger: str
    fiscal_year: int
    fiscal_period: int
    posting_date: date
    document_number: str
    line_item: str
    gl_account_code: str
    gl_account_name: str
    account_category: str
    debit_credit: str
    amount: Decimal
    currency: str
    amount_scale: int
    is_reversed: bool


@dataclass(frozen=True, slots=True)
class SapRefundEvidenceDraft:
    source_batch_key: str
    fiscal_year: int
    through_period: int
    company_codes: tuple[str, ...]
    lines: tuple[SapRefundLineDraft, ...]


@dataclass(frozen=True, slots=True)
class ImportSummary:
    source_version: str
    accepted_count: int
    replayed_count: int


@dataclass(frozen=True, slots=True)
class SapEvidenceImportSummary:
    source_batch_key: str
    accepted_count: int
    replayed_count: int
    complete_company_count: int


@dataclass(frozen=True, slots=True)
class IncomeTaxRefundResultView:
    target_id: UUID
    company_id: UUID
    company_code: str
    company_name: str
    refund_tax_year: int
    scan_period: str
    expected_refund_amount: Decimal
    currency: str
    receipt_status: str
    booking_status: str
    account_family: str | None
    receipt_source: str
    matched_amount: Decimal | None
    gl_account_code: str | None
    gl_account_name: str | None
    document_number: str | None
    line_item: str | None
    posting_date: date | None
    alert_code: str | None
    writeback_status: str | None


@dataclass(frozen=True, slots=True)
class IncomeTaxRefundSummaryView:
    refund_tax_year: int
    scan_period: str
    received_count: int
    not_received_count: int
    wrong_account_count: int
    ambiguous_count: int
    received: tuple[IncomeTaxRefundResultView, ...]
    not_received: tuple[IncomeTaxRefundResultView, ...]
    ambiguous: tuple[IncomeTaxRefundResultView, ...]


class IncomeTaxRefundService:
    def __init__(self, uow_factory: UowFactory) -> None:
        self._uow_factory = uow_factory

    def import_targets(
        self,
        *,
        refund_tax_year: int,
        source_version: str,
        drafts: Sequence[IncomeTaxRefundTargetDraft],
    ) -> ImportSummary:
        normalized_source_version = source_version.strip()
        if not normalized_source_version or len(normalized_source_version) > 128:
            raise IncomeTaxRefundServiceError(
                "INVALID_SOURCE_VERSION",
                "source_version must contain at most 128 nonblank characters",
            )
        if not drafts:
            raise IncomeTaxRefundServiceError(
                "EMPTY_REFUND_TARGET_IMPORT",
                "at least one refund target is required",
            )
        company_codes = tuple(draft.company_code.strip() for draft in drafts)
        if len(set(company_codes)) != len(company_codes):
            raise IncomeTaxRefundServiceError(
                "DUPLICATE_REFUND_TARGET",
                "one target per company and refund tax year is allowed",
            )
        accepted = 0
        replayed = 0
        with self._uow_factory() as uow:
            companies = _companies_by_code(uow, company_codes)
            _require_all_companies(companies, company_codes)
            for draft in drafts:
                if type(draft.received_in_source) is not bool:
                    raise IncomeTaxRefundServiceError(
                        "INVALID_REFUND_SOURCE_STATUS",
                        "received_in_source must be a boolean",
                    )
                company = companies[draft.company_code.strip()]
                source_record_key = draft.source_record_key.strip()
                if not source_record_key:
                    raise IncomeTaxRefundServiceError(
                        "INVALID_SOURCE_RECORD_KEY",
                        "source_record_key must be nonblank",
                    )
                amount = Money.unrounded(
                    draft.expected_refund_amount,
                    currency=draft.currency,
                    scale=draft.amount_scale,
                ).quantized()
                if amount.amount <= 0:
                    raise IncomeTaxRefundServiceError(
                        "INVALID_REFUND_AMOUNT",
                        "expected refund amount must be positive after quantization",
                    )
                existing = uow.session.execute(
                    select(IncomeTaxRefundTarget).where(
                        IncomeTaxRefundTarget.company_id == company.id,
                        IncomeTaxRefundTarget.refund_tax_year == refund_tax_year,
                    )
                ).scalar_one_or_none()
                if existing is None:
                    uow.session.add(
                        IncomeTaxRefundTarget(
                            company_id=company.id,
                            refund_tax_year=refund_tax_year,
                            source_record_key=source_record_key,
                            expected_amount=amount.amount,
                            currency=amount.currency,
                            amount_scale=amount.scale,
                            source_version=normalized_source_version,
                            receipt_status=("RECEIVED" if draft.received_in_source else "PENDING"),
                            received_at=(
                                datetime.now(timezone.utc) if draft.received_in_source else None
                            ),
                        )
                    )
                    accepted += 1
                    continue
                same_value = (
                    existing.expected_amount == amount.amount
                    and existing.currency == amount.currency
                    and existing.amount_scale == amount.scale
                )
                same_source = (
                    existing.source_record_key == source_record_key
                    and existing.source_version == normalized_source_version
                )
                if (
                    draft.received_in_source
                    and same_value
                    and existing.source_record_key == source_record_key
                ):
                    changed = (
                        existing.receipt_status != "RECEIVED"
                        or existing.source_version != normalized_source_version
                    )
                    existing.receipt_status = "RECEIVED"
                    existing.received_at = existing.received_at or datetime.now(timezone.utc)
                    existing.source_version = normalized_source_version
                    if changed:
                        accepted += 1
                    else:
                        replayed += 1
                    continue
                if same_value and same_source:
                    replayed += 1
                    continue
                if (
                    existing.receipt_status == "RECEIVED"
                    or uow.session.execute(
                        select(IncomeTaxRefundScanResult.id)
                        .where(IncomeTaxRefundScanResult.target_id == existing.id)
                        .limit(1)
                    ).scalar_one_or_none()
                    is not None
                ):
                    raise IncomeTaxRefundServiceError(
                        "REFUND_TARGET_IMMUTABLE",
                        "a scanned or received refund target cannot be changed",
                    )
                existing.source_record_key = source_record_key
                existing.expected_amount = amount.amount
                existing.currency = amount.currency
                existing.amount_scale = amount.scale
                existing.source_version = normalized_source_version
                if draft.received_in_source:
                    existing.receipt_status = "RECEIVED"
                    existing.received_at = datetime.now(timezone.utc)
                accepted += 1
            uow.commit()
        return ImportSummary(normalized_source_version, accepted, replayed)

    def import_sap_evidence(
        self,
        draft: SapRefundEvidenceDraft,
    ) -> SapEvidenceImportSummary:
        source_batch_key = draft.source_batch_key.strip()
        if not source_batch_key or len(source_batch_key) > 256:
            raise IncomeTaxRefundServiceError(
                "INVALID_SAP_EVIDENCE_BATCH_KEY",
                "source_batch_key must contain at most 256 nonblank characters",
            )
        scan_date = _month_end(draft.fiscal_year, draft.through_period)
        company_codes = tuple(value.strip() for value in draft.company_codes)
        if not company_codes or len(set(company_codes)) != len(company_codes):
            raise IncomeTaxRefundServiceError(
                "INVALID_EVIDENCE_COMPANY_SCOPE",
                "evidence company scope must be nonempty and unique",
            )
        line_codes = {line.company_code.strip() for line in draft.lines}
        if not line_codes <= set(company_codes):
            raise IncomeTaxRefundServiceError(
                "SAP_LINE_OUTSIDE_BATCH_SCOPE",
                "SAP evidence contains a company outside the declared complete scope",
            )
        for line in draft.lines:
            _validate_line_scope(line, draft.fiscal_year, draft.through_period)
        natural_keys = tuple(_line_natural_key(line) for line in draft.lines)
        if len(set(natural_keys)) != len(natural_keys):
            raise IncomeTaxRefundServiceError(
                "DUPLICATE_SAP_LINE_IN_BATCH",
                "SAP evidence contains a duplicate line natural key within the batch",
            )
        canonical_lines = tuple(_line_payload(line) for line in draft.lines)
        checksum = _canonical_sha256(
            {
                "fiscal_year": draft.fiscal_year,
                "through_period": draft.through_period,
                "company_codes": sorted(company_codes),
                "lines": sorted(canonical_lines, key=_canonical_json),
            }
        )
        accepted = 0
        replayed = 0
        with self._uow_factory() as uow:
            companies = _companies_by_code(uow, company_codes)
            _require_all_companies(companies, company_codes)
            existing_batch = uow.session.execute(
                select(SapRefundEvidenceBatch).where(
                    SapRefundEvidenceBatch.source_batch_key == source_batch_key
                )
            ).scalar_one_or_none()
            if existing_batch is not None:
                if existing_batch.checksum != checksum:
                    raise IncomeTaxRefundServiceError(
                        "SAP_EVIDENCE_BATCH_CONFLICT",
                        "source_batch_key was already used for different evidence",
                    )
                return SapEvidenceImportSummary(
                    source_batch_key,
                    0,
                    existing_batch.record_count,
                    len(existing_batch.company_ids),
                )
            batch = SapRefundEvidenceBatch(
                source_batch_key=source_batch_key,
                fiscal_year=draft.fiscal_year,
                through_period=scan_date,
                company_ids=sorted(str(companies[code].id) for code in company_codes),
                status="COMPLETE",
                record_count=len(draft.lines),
                checksum=checksum,
            )
            uow.session.add(batch)
            uow.session.flush()
            for line in draft.lines:
                company = companies[line.company_code.strip()]
                payload = _line_payload(line)
                source_hash = _canonical_sha256(payload)
                existing_line = uow.session.execute(
                    select(SapGlLineObservation).where(
                        SapGlLineObservation.source_batch_key == source_batch_key,
                        SapGlLineObservation.client == line.client.strip(),
                        SapGlLineObservation.ledger == line.ledger.strip(),
                        SapGlLineObservation.company_id == company.id,
                        SapGlLineObservation.fiscal_year == line.fiscal_year,
                        SapGlLineObservation.document_number == line.document_number.strip(),
                        SapGlLineObservation.line_item == line.line_item.strip(),
                    )
                ).scalar_one_or_none()
                if existing_line is not None:
                    if existing_line.source_hash != source_hash:
                        raise IncomeTaxRefundServiceError(
                            "SAP_LINE_CONTENT_CONFLICT",
                            "an SAP natural key was reused with different line content",
                        )
                    replayed += 1
                    continue
                amount = Money.unrounded(
                    line.amount,
                    currency=line.currency,
                    scale=line.amount_scale,
                ).quantized()
                uow.session.add(
                    SapGlLineObservation(
                        company_id=company.id,
                        source_batch_key=source_batch_key,
                        client=line.client.strip(),
                        ledger=line.ledger.strip(),
                        fiscal_year=line.fiscal_year,
                        fiscal_period=line.fiscal_period,
                        posting_date=line.posting_date,
                        document_number=line.document_number.strip(),
                        line_item=line.line_item.strip(),
                        gl_account_code=line.gl_account_code.strip(),
                        gl_account_name=line.gl_account_name.strip(),
                        account_category=line.account_category,
                        debit_credit=line.debit_credit,
                        amount=amount.amount,
                        currency=amount.currency,
                        amount_scale=amount.scale,
                        is_reversed=line.is_reversed,
                        source_hash=source_hash,
                    )
                )
                accepted += 1
            uow.commit()
        return SapEvidenceImportSummary(
            source_batch_key,
            accepted,
            replayed,
            len(company_codes),
        )

    def scan(
        self,
        *,
        refund_tax_year: int,
        scan_year: int,
        scan_month: int,
        source_batch_key: str,
        allowed_company_ids: frozenset[UUID] | None = None,
    ) -> IncomeTaxRefundSummaryView:
        scan_period = _refund_scan_period(scan_year, scan_month)
        if scan_year != refund_tax_year + 1:
            raise IncomeTaxRefundServiceError(
                "INVALID_REFUND_SCAN_YEAR",
                "scan year must equal refund tax year plus one",
            )
        scan_date = _month_end(scan_year, scan_month)
        with self._uow_factory() as uow:
            evidence = uow.session.execute(
                select(SapRefundEvidenceBatch).where(
                    SapRefundEvidenceBatch.source_batch_key == source_batch_key.strip()
                )
            ).scalar_one_or_none()
            if evidence is None:
                raise IncomeTaxRefundServiceError(
                    "SAP_EVIDENCE_BATCH_NOT_FOUND",
                    "the completed SAP evidence batch was not found",
                )
            if (
                evidence.status != "COMPLETE"
                or evidence.fiscal_year != scan_year
                or evidence.through_period < scan_date
            ):
                raise IncomeTaxRefundServiceError(
                    "SAP_EVIDENCE_INCOMPLETE",
                    "SAP evidence does not prove complete coverage through the scan month",
                )
            statement = (
                select(IncomeTaxRefundTarget)
                .where(
                    IncomeTaxRefundTarget.refund_tax_year == refund_tax_year,
                    IncomeTaxRefundTarget.receipt_status == "PENDING",
                )
                .order_by(
                    IncomeTaxRefundTarget.company_id,
                    IncomeTaxRefundTarget.id,
                )
                .with_for_update()
            )
            if allowed_company_ids is not None:
                statement = statement.where(
                    IncomeTaxRefundTarget.company_id.in_(allowed_company_ids)
                )
            targets = tuple(uow.session.scalars(statement).all())
            existing_scan_statement = (
                select(IncomeTaxRefundScanResult)
                .join(
                    IncomeTaxRefundTarget,
                    IncomeTaxRefundTarget.id == IncomeTaxRefundScanResult.target_id,
                )
                .where(
                    IncomeTaxRefundTarget.refund_tax_year == refund_tax_year,
                    IncomeTaxRefundScanResult.scan_period == scan_date,
                )
            )
            if allowed_company_ids is not None:
                existing_scan_statement = existing_scan_statement.where(
                    IncomeTaxRefundScanResult.company_id.in_(allowed_company_ids)
                )
            for prior_result in uow.session.scalars(existing_scan_statement):
                if (
                    prior_result.structured_output.get("source_batch_key")
                    != source_batch_key.strip()
                ):
                    raise IncomeTaxRefundServiceError(
                        "REFUND_SCAN_CONFLICT",
                        "the monthly scan already used a different SAP evidence batch",
                    )
            companies = {
                company.id: company
                for company in uow.session.scalars(
                    select(Company).where(Company.id.in_(target.company_id for target in targets))
                ).all()
            }
            complete_company_ids = {UUID(value) for value in evidence.company_ids}
            missing = [
                target.company_id
                for target in targets
                if target.company_id not in complete_company_ids
            ]
            if missing:
                raise IncomeTaxRefundServiceError(
                    "SAP_EVIDENCE_COMPANY_INCOMPLETE",
                    "SAP evidence is not complete for every pending refund company",
                )
            for target in targets:
                existing = uow.session.execute(
                    select(IncomeTaxRefundScanResult).where(
                        IncomeTaxRefundScanResult.target_id == target.id,
                        IncomeTaxRefundScanResult.scan_period == scan_date,
                    )
                ).scalar_one_or_none()
                if existing is not None:
                    if (
                        existing.structured_output.get("source_batch_key")
                        != source_batch_key.strip()
                    ):
                        raise IncomeTaxRefundServiceError(
                            "REFUND_SCAN_CONFLICT",
                            "the monthly scan already used a different SAP evidence batch",
                        )
                    continue
                lines = tuple(
                    uow.session.scalars(
                        select(SapGlLineObservation).where(
                            SapGlLineObservation.source_batch_key == source_batch_key.strip(),
                            SapGlLineObservation.company_id == target.company_id,
                            SapGlLineObservation.fiscal_year == scan_year,
                            SapGlLineObservation.fiscal_period <= scan_month,
                            SapGlLineObservation.currency == target.currency,
                            SapGlLineObservation.amount_scale == target.amount_scale,
                            SapGlLineObservation.amount > 0,
                        )
                    ).all()
                )
                candidates = tuple(_candidate(line) for line in lines)
                result = evaluate_income_tax_refund(
                    IncomeTaxRefundInputs(
                        refund_tax_year=refund_tax_year,
                        scan_period=scan_period,
                        expected_refund_amount=Money.unrounded(
                            target.expected_amount,
                            currency=target.currency,
                            scale=target.amount_scale,
                        ),
                        candidates=candidates,
                    )
                )
                matched_line = (
                    next(
                        line
                        for line in lines
                        if str(line.id) == result.matched_candidates[0].line_id
                    )
                    if len(result.matched_candidates) == 1
                    else None
                )
                structured_output = {
                    "completeness": True,
                    "source_batch_key": source_batch_key.strip(),
                    "refund_tax_year": refund_tax_year,
                    "scan_period": f"{scan_year:04d}-{scan_month:02d}",
                    "match_count": len(result.matched_candidates),
                    "match_stage": (
                        result.match_stage.value if result.match_stage is not None else None
                    ),
                    "matched_account_family": (
                        result.matched_candidates[0].account_family.value
                        if len(result.matched_candidates) == 1
                        else None
                    ),
                    "continue_scanning": result.continue_scanning,
                    "requires_writeback": result.requires_writeback,
                    "matched_candidates": [
                        _candidate_evidence(candidate) for candidate in result.matched_candidates
                    ],
                }
                scan_result = IncomeTaxRefundScanResult(
                    target_id=target.id,
                    company_id=target.company_id,
                    scan_period=scan_date,
                    receipt_status=str(result.receipt_status),
                    account_status=str(result.booking_status),
                    matched_line_id=matched_line.id if matched_line is not None else None,
                    expected_amount=result.normalized_expected_refund_amount.amount,
                    matched_amount=(
                        result.matched_candidates[0].amount.quantized().amount
                        if matched_line is not None
                        else None
                    ),
                    gl_account_code=(
                        matched_line.gl_account_code if matched_line is not None else None
                    ),
                    gl_account_name=(
                        matched_line.gl_account_name if matched_line is not None else None
                    ),
                    alert_code=result.alert_code,
                    structured_output=structured_output,
                )
                uow.session.add(scan_result)
                target.latest_scan_period = scan_date
                if result.risk_case_required:
                    if result.alert_code is None:
                        raise RuntimeError("risk-case result must have an alert code")
                    if (
                        result.booking_status is RefundBookingStatus.WRONG_ACCOUNT
                        and matched_line is None
                    ):
                        raise RuntimeError("wrong-account result must have one matched SAP line")
                    uow.session.flush()
                    company = companies[target.company_id]
                    fingerprint = _refund_case_fingerprint(
                        company.company_code,
                        target.refund_tax_year,
                    )
                    if uow.risks.get_case_by_fingerprint(fingerprint) is None:
                        lineage: dict[str, object] = {
                            "refund_tax_year": target.refund_tax_year,
                            "scan_year": scan_year,
                            "scan_month": scan_month,
                            "scan_period": f"{scan_year:04d}-{scan_month:02d}",
                            "target_id": str(target.id),
                            "scan_result_id": str(scan_result.id),
                            "source_batch_key": source_batch_key.strip(),
                            "matched_candidates": [
                                _candidate_evidence(candidate)
                                for candidate in result.matched_candidates
                            ],
                        }
                        if matched_line is not None:
                            lineage.update(
                                {
                                    "matched_line_id": str(matched_line.id),
                                    "document_number": matched_line.document_number,
                                    "line_item": matched_line.line_item,
                                    "gl_account_code": matched_line.gl_account_code,
                                    "gl_account_name": matched_line.gl_account_name,
                                    "account_category": matched_line.account_category,
                                }
                            )
                        uow.risks.add_case(
                            RiskCase(
                                fingerprint=fingerprint,
                                company_id=target.company_id,
                                latest_detection_id=None,
                                monitor_type=(MonitorType.INCOME_TAX_REFUND_ACCOUNT_ACCURACY),
                                status=RiskCaseStatus.NEW,
                                risk_amount=(result.normalized_expected_refund_amount.amount),
                                risk_rate=None,
                                currency=target.currency,
                                amount_scale=target.amount_scale,
                                risk_direction=result.alert_code,
                                priority=2,
                                assignee=None,
                                merged_into_case_id=None,
                                lineage=lineage,
                                row_version=1,
                            )
                        )
                if result.receipt_status is RefundReceiptStatus.RECEIVED:
                    target.receipt_status = "RECEIVED"
                    target.received_at = datetime.now(timezone.utc)
                    existing_writeback = uow.session.execute(
                        select(IncomeTaxRefundWriteback).where(
                            IncomeTaxRefundWriteback.target_id == target.id
                        )
                    ).scalar_one_or_none()
                    if existing_writeback is None:
                        uow.session.add(
                            IncomeTaxRefundWriteback(
                                target_id=target.id,
                                company_id=target.company_id,
                                idempotency_key=f"refund-received:{target.id}",
                                desired_value="已退税",
                                status="PENDING",
                                attempt_count=0,
                            )
                        )
            uow.commit()
        return self.list_results(
            refund_tax_year=refund_tax_year,
            scan_year=scan_year,
            scan_month=scan_month,
            allowed_company_ids=allowed_company_ids,
        )

    def list_results(
        self,
        *,
        refund_tax_year: int,
        scan_year: int,
        scan_month: int,
        allowed_company_ids: frozenset[UUID] | None = None,
    ) -> IncomeTaxRefundSummaryView:
        if scan_year != refund_tax_year + 1:
            raise IncomeTaxRefundServiceError(
                "INVALID_REFUND_SCAN_YEAR",
                "scan year must equal refund tax year plus one",
            )
        scan_period = _refund_scan_period(scan_year, scan_month)
        scan_date = _month_end(scan_period.year, scan_period.month)
        with self._uow_factory() as uow:
            statement = (
                select(IncomeTaxRefundTarget, Company)
                .join(Company, Company.id == IncomeTaxRefundTarget.company_id)
                .where(IncomeTaxRefundTarget.refund_tax_year == refund_tax_year)
                .order_by(Company.company_code)
            )
            if allowed_company_ids is not None:
                statement = statement.where(Company.id.in_(allowed_company_ids))
            target_rows = tuple(uow.session.execute(statement).all())
            target_ids = tuple(target.id for target, _company in target_rows)
            result_rows = (
                tuple(
                    uow.session.scalars(
                        select(IncomeTaxRefundScanResult)
                        .where(
                            IncomeTaxRefundScanResult.target_id.in_(target_ids),
                            IncomeTaxRefundScanResult.scan_period <= scan_date,
                        )
                        .order_by(
                            IncomeTaxRefundScanResult.target_id,
                            IncomeTaxRefundScanResult.scan_period.desc(),
                        )
                    ).all()
                )
                if target_ids
                else ()
            )
            latest_by_target: dict[UUID, IncomeTaxRefundScanResult] = {}
            for scan_result in result_rows:
                latest_by_target.setdefault(scan_result.target_id, scan_result)
            matched_ids = {
                result.matched_line_id
                for result in latest_by_target.values()
                if result.matched_line_id is not None
            }
            lines = (
                {
                    line.id: line
                    for line in uow.session.scalars(
                        select(SapGlLineObservation).where(SapGlLineObservation.id.in_(matched_ids))
                    ).all()
                }
                if matched_ids
                else {}
            )
            writebacks = (
                {
                    row.target_id: row
                    for row in uow.session.scalars(
                        select(IncomeTaxRefundWriteback).where(
                            IncomeTaxRefundWriteback.target_id.in_(target_ids)
                        )
                    ).all()
                }
                if target_ids
                else {}
            )
            received: list[IncomeTaxRefundResultView] = []
            not_received: list[IncomeTaxRefundResultView] = []
            ambiguous: list[IncomeTaxRefundResultView] = []
            for target, company in target_rows:
                latest_result = latest_by_target.get(target.id)
                if target.receipt_status == "RECEIVED" and (
                    latest_result is None or latest_result.receipt_status != "RECEIVED"
                ):
                    received.append(
                        IncomeTaxRefundResultView(
                            target_id=target.id,
                            company_id=target.company_id,
                            company_code=company.company_code,
                            company_name=company.company_name,
                            refund_tax_year=target.refund_tax_year,
                            scan_period=f"{scan_year:04d}-{scan_month:02d}",
                            expected_refund_amount=target.expected_amount,
                            currency=target.currency,
                            receipt_status="RECEIVED",
                            booking_status="NOT_APPLICABLE",
                            account_family=None,
                            receipt_source="LARK_MANUAL",
                            matched_amount=None,
                            gl_account_code=None,
                            gl_account_name=None,
                            document_number=None,
                            line_item=None,
                            posting_date=None,
                            alert_code=None,
                            writeback_status=None,
                        )
                    )
                    continue
                if latest_result is None:
                    continue
                if (
                    latest_result.receipt_status != "RECEIVED"
                    and latest_result.scan_period != scan_date
                ):
                    continue
                line = (
                    lines.get(latest_result.matched_line_id)
                    if latest_result.matched_line_id is not None
                    else None
                )
                writeback = writebacks.get(target.id)
                view = IncomeTaxRefundResultView(
                    target_id=target.id,
                    company_id=target.company_id,
                    company_code=company.company_code,
                    company_name=company.company_name,
                    refund_tax_year=target.refund_tax_year,
                    scan_period=(
                        f"{latest_result.scan_period.year:04d}-"
                        f"{latest_result.scan_period.month:02d}"
                    ),
                    expected_refund_amount=latest_result.expected_amount,
                    currency=target.currency,
                    receipt_status=latest_result.receipt_status,
                    booking_status=latest_result.account_status,
                    account_family=line.account_category if line is not None else None,
                    receipt_source="SAP_MATCH",
                    matched_amount=latest_result.matched_amount,
                    gl_account_code=latest_result.gl_account_code,
                    gl_account_name=latest_result.gl_account_name,
                    document_number=line.document_number if line is not None else None,
                    line_item=line.line_item if line is not None else None,
                    posting_date=line.posting_date if line is not None else None,
                    alert_code=latest_result.alert_code,
                    writeback_status=writeback.status if writeback is not None else None,
                )
                if latest_result.receipt_status == "RECEIVED":
                    received.append(view)
                elif latest_result.receipt_status == "NOT_RECEIVED":
                    not_received.append(view)
                else:
                    ambiguous.append(view)
        return IncomeTaxRefundSummaryView(
            refund_tax_year=refund_tax_year,
            scan_period=f"{scan_year:04d}-{scan_month:02d}",
            received_count=len(received),
            not_received_count=len(not_received),
            wrong_account_count=sum(item.booking_status == "WRONG_ACCOUNT" for item in received),
            ambiguous_count=len(ambiguous),
            received=tuple(received),
            not_received=tuple(not_received),
            ambiguous=tuple(ambiguous),
        )


def _companies_by_code(
    uow: UnitOfWork,
    company_codes: Iterable[str],
) -> dict[str, Company]:
    normalized = tuple(dict.fromkeys(value.strip() for value in company_codes))
    return {
        company.company_code: company
        for company in uow.session.scalars(
            select(Company).where(Company.company_code.in_(normalized))
        ).all()
    }


def _require_all_companies(
    companies: dict[str, Company],
    company_codes: Iterable[str],
) -> None:
    missing = sorted(set(company_codes) - companies.keys())
    if missing:
        raise IncomeTaxRefundServiceError(
            "REFUND_COMPANY_NOT_FOUND",
            f"company master is missing {len(missing)} requested company code(s)",
        )


def _month_end(year: int, month: int) -> date:
    if not 1 <= month <= 12:
        raise IncomeTaxRefundServiceError(
            "INVALID_FISCAL_PERIOD",
            "fiscal period must be between 1 and 12",
        )
    return date(year, month, monthrange(year, month)[1])


def _refund_scan_period(year: int, month: int) -> RefundScanPeriod:
    try:
        return RefundScanPeriod(year, month)
    except (TypeError, ValueError) as error:
        raise IncomeTaxRefundServiceError(
            "INVALID_REFUND_SCAN_PERIOD",
            str(error),
        ) from error


def _validate_line_scope(line: SapRefundLineDraft, fiscal_year: int, through: int) -> None:
    if (
        line.fiscal_year != fiscal_year
        or line.fiscal_period > through
        or line.posting_date.year != line.fiscal_year
        or line.posting_date.month != line.fiscal_period
    ):
        raise IncomeTaxRefundServiceError(
            "SAP_LINE_PERIOD_MISMATCH",
            "SAP line period is outside the declared complete evidence range",
        )
    if line.account_category not in {item.value for item in RefundAccountFamily}:
        raise IncomeTaxRefundServiceError(
            "INVALID_REFUND_ACCOUNT_CATEGORY",
            "SAP refund line uses an unsupported account category",
        )
    if line.debit_credit not in {"DEBIT", "CREDIT"}:
        raise IncomeTaxRefundServiceError(
            "INVALID_DEBIT_CREDIT",
            "SAP refund line debit_credit must be DEBIT or CREDIT",
        )


def _candidate(line: SapGlLineObservation) -> IncomeTaxRefundCandidate:
    return IncomeTaxRefundCandidate(
        line_id=str(line.id),
        account_family=RefundAccountFamily(line.account_category),
        account_code=line.gl_account_code,
        account_name=line.gl_account_name,
        document_number=line.document_number,
        line_item=line.line_item,
        posting_date=line.posting_date,
        amount=Money.unrounded(
            line.amount,
            currency=line.currency,
            scale=line.amount_scale,
        ),
        is_credit=line.debit_credit == "CREDIT",
        is_reversed=line.is_reversed,
    )


def _candidate_evidence(candidate: IncomeTaxRefundCandidate) -> dict[str, object]:
    return {
        "line_id": candidate.line_id,
        "account_family": str(candidate.account_family),
        "account_code": candidate.account_code,
        "account_name": candidate.account_name,
        "document_number": candidate.document_number,
        "line_item": candidate.line_item,
        "posting_date": candidate.posting_date.isoformat(),
        "amount": format(candidate.amount.quantized().amount, "f"),
    }


def _line_payload(line: SapRefundLineDraft) -> dict[str, object]:
    amount = Money.unrounded(
        line.amount,
        currency=line.currency,
        scale=line.amount_scale,
    ).quantized()
    return {
        "company_code": line.company_code.strip(),
        "client": line.client.strip(),
        "ledger": line.ledger.strip(),
        "fiscal_year": line.fiscal_year,
        "fiscal_period": line.fiscal_period,
        "posting_date": line.posting_date.isoformat(),
        "document_number": line.document_number.strip(),
        "line_item": line.line_item.strip(),
        "gl_account_code": line.gl_account_code.strip(),
        "gl_account_name": line.gl_account_name.strip(),
        "account_category": line.account_category,
        "debit_credit": line.debit_credit,
        "amount": format(amount.amount, "f"),
        "currency": amount.currency,
        "amount_scale": amount.scale,
        "is_reversed": line.is_reversed,
    }


def _line_natural_key(line: SapRefundLineDraft) -> tuple[str, str, str, int, str, str]:
    return (
        line.client.strip(),
        line.ledger.strip(),
        line.company_code.strip(),
        line.fiscal_year,
        line.document_number.strip(),
        line.line_item.strip(),
    )


def _refund_case_fingerprint(company_code: str, refund_tax_year: int) -> str:
    canonical = "|".join(
        (
            company_code,
            str(refund_tax_year),
            MonitorType.INCOME_TAX_REFUND_ACCOUNT_ACCURACY.value,
        )
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _canonical_sha256(value: object) -> str:
    return sha256(_canonical_json(value).encode("utf-8")).hexdigest()


__all__ = [
    "ImportSummary",
    "IncomeTaxRefundResultView",
    "IncomeTaxRefundService",
    "IncomeTaxRefundServiceError",
    "IncomeTaxRefundSummaryView",
    "IncomeTaxRefundTargetDraft",
    "SapEvidenceImportSummary",
    "SapRefundEvidenceDraft",
    "SapRefundLineDraft",
]
