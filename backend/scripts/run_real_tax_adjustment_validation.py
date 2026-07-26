"""Run the real, company-wide welfare and donation account-accuracy check."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
import csv
from datetime import UTC, datetime
from decimal import Decimal
import json
from pathlib import Path
import sys
from time import monotonic


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPO_ROOT / "backend"
BACKEND_SRC = REPO_ROOT / "backend" / "src"
BACKEND_SCRIPTS = REPO_ROOT / "backend" / "scripts"
for import_path in (BACKEND_ROOT, BACKEND_SRC, BACKEND_SCRIPTS):
    if str(import_path) not in sys.path:
        sys.path.insert(0, str(import_path))

from scripts import run_real_full_validation as full_validation  # noqa: E402
from scripts.archive_feishu_alert_results import archive_report  # noqa: E402
from scripts.enqueue_feishu_alert_notifications import enqueue_report  # noqa: E402
from tax_risk.adapters.cache.memory_fetch_cache import MemoryFetchCache  # noqa: E402
from tax_risk.adapters.dgc.hesi_business_entertainment import (  # noqa: E402
    HesiApplicationClient,
    HesiApplicationClientConfiguration,
    HesiDetailClient,
    HesiDetailClientConfiguration,
    HesiInvoiceClient,
    HesiInvoiceClientConfiguration,
)
from tax_risk.adapters.ingest.dgc_sap_profit import (  # noqa: E402
    DgcSapProfitClient,
    _pinned_tls_context,
)
from tax_risk.application.external_fetch import (  # noqa: E402
    FetchCoordinatorConfig,
    FetchRequest,
    ParallelFetchCoordinator,
)
from tax_risk.application.tax_adjustment_accounts.adjustment import (  # noqa: E402
    calculate_donation_adjustment,
    calculate_welfare_adjustment,
)
from tax_risk.application.tax_adjustment_accounts.business_entertainment import (  # noqa: E402
    BusinessEntertainmentAccountCheckService,
    BusinessEntertainmentCheckRequest,
    BusinessEntertainmentCheckedDetail,
    BusinessEntertainmentLabel,
    HesiApplicationRow,
    HesiDetailRow,
    HesiInvoiceRow,
    business_entertainment_account_is_in_scope,
    extract_hesi_document_code,
)
from tax_risk.application.tax_adjustment_accounts.contracts import (  # noqa: E402
    AccountCheckRequest,
    AdjustmentSubject,
    CheckStatus,
    SapIncomeRow,
    SettlementAdjustmentRow,
    TrialBalanceRow,
)
from tax_risk.application.tax_adjustment_accounts.rules import (  # noqa: E402
    account_is_in_scope,
    classify_detail,
    recommended_accounts,
)
from tax_risk.application.tax_adjustment_accounts.service import (  # noqa: E402
    TaxAdjustmentAccountCheckService,
)
from tax_risk.application.tax_adjustment_accounts.web_report import (  # noqa: E402
    merge_tax_adjustment_report,
)
from tax_risk.config import Settings  # noqa: E402


SETTLEMENT_SOURCE = "dgc_sap_dividend_detail"
TRIAL_BALANCE_SOURCE = "dgc_sap_trial_balance"
SAP_INCOME_SOURCE = "dgc_sap_profit"
HESI_DETAIL_SOURCE = "dgc_hesi_reimbursement"
HESI_INVOICE_SOURCE = "dgc_hesi_invoice"

BUSINESS_LABELS = {
    BusinessEntertainmentLabel.REASONABLE: "业务招待费入账合理",
    BusinessEntertainmentLabel.EMPLOYEE_WELFARE: "可能应归福利费",
    BusinessEntertainmentLabel.MEETING_OR_EDUCATION: "可能应归会议费或职工教育经费",
}
BUSINESS_RECOMMENDATIONS = {
    BusinessEntertainmentLabel.EMPLOYEE_WELFARE: "福利费",
    BusinessEntertainmentLabel.MEETING_OR_EDUCATION: "会议费或职工教育经费",
}


def _fetch_many(
    coordinator: ParallelFetchCoordinator,
    requests: Mapping[str, FetchRequest],
    *,
    max_workers: int,
    label: str,
) -> tuple[dict[str, tuple[Mapping[str, object], ...]], dict[str, str]]:
    results: dict[str, tuple[Mapping[str, object], ...]] = {}
    errors: dict[str, str] = {}
    completed = 0
    total = len(requests)
    if total == 0:
        return results, errors
    with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=label) as pool:
        futures = {
            pool.submit(coordinator.fetch_one, request): key
            for key, request in requests.items()
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                outcome = future.result()
                results[key] = tuple(outcome.result.records)
            except Exception as error:
                error_code = getattr(error, "error_code", type(error).__name__)
                errors[key] = str(error_code)[:128]
            completed += 1
            if completed % 25 == 0 or completed == total:
                print(f"{label} progress: {completed}/{total}", flush=True)
    return results, errors


def _settlement_rows(
    records: tuple[Mapping[str, object], ...],
    *,
    company_code: str,
    fiscal_year: str,
) -> tuple[SettlementAdjustmentRow, ...]:
    rows: list[SettlementAdjustmentRow] = []
    for record in records:
        raw = dict(record)
        raw.setdefault("company", company_code)
        raw.setdefault("companyname", None)
        row = SettlementAdjustmentRow.model_validate(raw)
        if row.company != company_code or row.fiscal_year != fiscal_year:
            raise ValueError("settlement_adjustment escaped the requested scope")
        rows.append(row)
    return tuple(rows)


def _trial_balance_rows(
    records: tuple[Mapping[str, object], ...],
    *,
    company_code: str,
    fiscal_year: str,
    fiscal_period: str,
) -> tuple[TrialBalanceRow, ...]:
    rows = tuple(TrialBalanceRow.model_validate(dict(record)) for record in records)
    if any(
        row.company_code != company_code
        or row.fiscal_year != fiscal_year
        or row.fiscal_period != fiscal_period
        for row in rows
    ):
        raise ValueError("trial_balance escaped the requested scope")
    return rows


def _income_rows(
    records: tuple[Mapping[str, object], ...],
    *,
    company_code: str,
    fiscal_year: str,
    fiscal_period: str,
) -> tuple[SapIncomeRow, ...]:
    rows = tuple(SapIncomeRow.model_validate(dict(record)) for record in records)
    if any(
        row.bukrs != company_code
        or row.gjahr != fiscal_year
        or row.monat != fiscal_period
        for row in rows
    ):
        raise ValueError("sapincome escaped the requested scope")
    return rows


def _hesi_detail_rows(
    records: tuple[Mapping[str, object], ...],
    *,
    company_code: str,
) -> tuple[HesiDetailRow, ...]:
    rows = tuple(
        HesiDetailRow(
            company_code=str(record.get("company_code") or ""),
            document_code=str(record.get("expense_code") or record.get("code") or ""),
            description=str(record.get("description") or ""),
        )
        for record in records
    )
    if any(row.company_code != company_code for row in rows):
        raise ValueError("hesimingxi escaped the requested company scope")
    return rows


def _hesi_invoice_rows(
    records: tuple[Mapping[str, object], ...],
    *,
    company_code: str,
) -> tuple[HesiInvoiceRow, ...]:
    rows = tuple(
        HesiInvoiceRow(
            company_code=str(record.get("company_code") or ""),
            code=str(record.get("code") or ""),
            invoice_id=str(record.get("invoice_id") or ""),
            reception_apply_code=str(record.get("reception_apply_code") or ""),
        )
        for record in records
    )
    if any(row.company_code != company_code for row in rows):
        raise ValueError("hesiinvoice escaped the requested company scope")
    return rows


class _SettlementMapSource:
    def __init__(self, rows: Mapping[str, tuple[SettlementAdjustmentRow, ...]]) -> None:
        self._rows = rows

    def fetch_rows(
        self,
        *,
        company: str,
        fiscal_year: str,
    ) -> tuple[SettlementAdjustmentRow, ...]:
        rows = self._rows.get(company, ())
        if any(row.fiscal_year != fiscal_year for row in rows):
            raise ValueError("settlement rows escaped the requested year scope")
        return rows


class _HesiDetailMapSource:
    def __init__(
        self,
        rows: Mapping[str, tuple[HesiDetailRow, ...]],
        errors: Mapping[str, str],
    ) -> None:
        self._rows = rows
        self._errors = errors

    def fetch_rows(self, *, company_code: str) -> tuple[HesiDetailRow, ...]:
        if company_code in self._errors:
            raise RuntimeError(self._errors[company_code])
        return self._rows.get(company_code, ())


class _HesiInvoiceMapSource:
    def __init__(
        self,
        rows: Mapping[str, tuple[HesiInvoiceRow, ...]],
        errors: Mapping[str, str],
    ) -> None:
        self._rows = rows
        self._errors = errors

    def fetch_rows(self, *, company_code: str) -> tuple[HesiInvoiceRow, ...]:
        if company_code in self._errors:
            raise RuntimeError(self._errors[company_code])
        return self._rows.get(company_code, ())


class _UnavailableApplicationSource:
    def fetch_rows(self, *, company_code: str) -> tuple[HesiApplicationRow, ...]:
        del company_code
        raise RuntimeError("HESI_APPLICATION_SOURCE_NOT_CONFIGURED")


class _HesiApplicationMapSource:
    def __init__(
        self,
        rows: Mapping[str, tuple[HesiApplicationRow, ...]],
        errors: Mapping[str, str],
    ) -> None:
        self._rows = rows
        self._errors = errors

    def fetch_rows(self, *, company_code: str) -> tuple[HesiApplicationRow, ...]:
        if company_code in self._errors:
            raise RuntimeError(self._errors[company_code])
        return self._rows.get(company_code, ())


def _fetch_company_rows[RowT](
    fetch_rows: Callable[..., tuple[RowT, ...]] | None,
    company_codes: tuple[str, ...],
    *,
    max_workers: int,
    label: str,
    unavailable_error: str,
) -> tuple[dict[str, tuple[RowT, ...]], dict[str, str]]:
    rows: dict[str, tuple[RowT, ...]] = {}
    errors: dict[str, str] = {}
    if fetch_rows is None:
        return {}, {
            company_code: unavailable_error
            for company_code in company_codes
        }
    with ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix=label,
    ) as pool:
        futures = {
            pool.submit(fetch_rows, company_code=company_code): company_code
            for company_code in company_codes
        }
        for completed, future in enumerate(as_completed(futures), start=1):
            company_code = futures[future]
            try:
                rows[company_code] = tuple(future.result())
            except Exception as error:
                errors[company_code] = _error_code(error)
            if completed % 25 == 0 or completed == len(futures):
                print(
                    f"{label} progress: {completed}/{len(futures)}",
                    flush=True,
                )
    return rows, errors


def _json_row(row: SettlementAdjustmentRow) -> dict[str, object]:
    payload = json.loads(row.model_dump_json())
    assert isinstance(payload, dict)
    return payload


def _atomic_json(path: Path, payload: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def _write_csv(path: Path, rows: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    fieldnames = (
        list(dict.fromkeys(key for row in rows for key in row))
        if rows
        else ["company_code"]
    )
    with temporary.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    temporary.replace(path)


def _error_code(error: Exception) -> str:
    stable_code = getattr(error, "error_code", None)
    if stable_code is not None:
        return str(stable_code)[:128]
    message = str(error).strip()
    if message and len(message) <= 128 and all(
        character.isupper() or character.isdigit() or character == "_"
        for character in message
    ):
        return message
    return type(error).__name__


def _int_value(value: object) -> int:
    return int(str(value))


def _result_template(
    *,
    sequence: int,
    company_code: str,
    company_name: str,
    fiscal_year: str,
    through_month: int,
    source_row_count: int,
    welfare_cumulative: Decimal,
    donation_cumulative: Decimal,
    welfare_candidate_count: int,
    donation_candidate_count: int,
    business_entertainment_cumulative: Decimal,
    business_entertainment_detail_count: int,
) -> dict[str, object]:
    return {
        "sequence": sequence,
        "company_code": company_code,
        "company_name": company_name,
        "fiscal_year": fiscal_year,
        "through_month": through_month,
        "welfare_status": "CLEAR",
        "welfare_reason": "NO_ABNORMAL_CANDIDATE",
        "welfare_cumulative": format(welfare_cumulative, "f"),
        "salary_cumulative": "",
        "welfare_deduction_limit": "",
        "welfare_adjustment": "",
        "welfare_detail_selected": "false",
        "welfare_abnormal_candidate_count": welfare_candidate_count,
        "welfare_alert_count": 0,
        "welfare_alert_amount": "0",
        "welfare_error": "",
        "donation_status": "CLEAR",
        "donation_reason": "NO_ABNORMAL_CANDIDATE",
        "donation_cumulative": format(donation_cumulative, "f"),
        "donation_abnormal_candidate_count": donation_candidate_count,
        "donation_alert_count": 0,
        "donation_error": "",
        "business_entertainment_status": "CLEAR",
        "business_entertainment_reason": "NO_IN_SCOPE_DETAILS",
        "business_entertainment_cumulative": format(
            business_entertainment_cumulative, "f"
        ),
        "business_entertainment_detail_count": business_entertainment_detail_count,
        "business_entertainment_alert_count": 0,
        "business_entertainment_alert_amount": "0",
        "business_entertainment_hesi_detail_count": 0,
        "business_entertainment_hesi_invoice_count": 0,
        "business_entertainment_hesi_application_count": 0,
        "business_entertainment_evidence_status": (
            "NOT_REQUIRED" if business_entertainment_detail_count == 0 else "PENDING"
        ),
        "business_entertainment_error": "",
        "source_row_count": source_row_count,
    }


def _alert_detail(
    *,
    sequence: int,
    company_name: str,
    subject: AdjustmentSubject,
    row: SettlementAdjustmentRow,
) -> dict[str, object]:
    decision = classify_detail(subject, row.detail_text)
    return {
        "sequence": sequence,
        "company_code": row.company,
        "company_name": company_name,
        "subject": subject.value,
        "fiscal_period": row.fiscal_period,
        "voucher_no": row.voucher_no,
        "original_system_doc_no": row.original_system_doc_no,
        "gl_account": row.gl_account,
        "account_name": row.account_name,
        "header_text": row.header_text,
        "detail_text": row.detail_text,
        "amount": format(row.amount_ksl, "f"),
        "currency": row.group_currency,
        "matched_keywords": "|".join(decision.matched_keywords),
        "recommended_account": "|".join(recommended_accounts(decision.labels)),
    }


def _business_candidate(
    *,
    sequence: int,
    company_name: str,
    detail: BusinessEntertainmentCheckedDetail,
) -> dict[str, object]:
    row = detail.row
    recommendations = tuple(
        dict.fromkeys(
            BUSINESS_RECOMMENDATIONS[label]
            for label in detail.labels
            if label in BUSINESS_RECOMMENDATIONS
        )
    )
    return {
        "candidate_no": str(sequence),
        "company_code": row.company,
        "company_name": company_name,
        "subject": "业务招待费",
        "fiscal_period": row.fiscal_period,
        "voucher_no": row.voucher_no,
        "original_system_doc_no": row.original_system_doc_no,
        "gl_account": row.gl_account,
        "account_name": row.account_name,
        "header_text": row.header_text,
        "detail_text": row.detail_text,
        "amount": format(row.amount_ksl, "f"),
        "currency": row.group_currency,
        "classification": "、".join(BUSINESS_LABELS[label] for label in detail.labels),
        "matched_keywords": "、".join(detail.matched_keywords),
        "recommended_account": "、".join(recommendations),
        "recommendation_basis": (
            f"{detail.decision_source.value}命中关键词："
            f"{'、'.join(detail.matched_keywords)}"
        ),
        "decision_source": detail.decision_source.value,
        "evaluated_sources": "、".join(
            source.value for source in detail.evaluated_sources
        ),
        "evidence_texts": " | ".join(detail.evidence_texts),
        "hesi_document_code": detail.hesi_document_code or "",
        "hesi_detail_descriptions": " | ".join(detail.hesi_detail_descriptions),
        "hesi_application_descriptions": " | ".join(
            detail.hesi_application_descriptions
        ),
        "hesi_detail_match_count": str(detail.hesi_detail_match_count),
        "hesi_invoice_match_count": str(detail.hesi_invoice_match_count),
        "reception_apply_codes": "、".join(detail.reception_apply_codes),
        "hesi_application_match_count": str(detail.hesi_application_match_count),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fiscal-year", default="2026")
    parser.add_argument("--through-month", type=int, default=6, choices=range(1, 13))
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=REPO_ROOT / "artifacts" / "acceptance",
    )
    parser.add_argument(
        "--web-report",
        type=Path,
        default=REPO_ROOT / "web" / "public" / "real-validation-latest.json",
    )
    parser.add_argument("--skip-alert-archive", action="store_true")
    parser.add_argument("--archive-base-as", choices=("user", "bot"), default="user")
    parser.add_argument("--archive-base-profile", default="tax-risk-notifier")
    parser.add_argument("--skip-alert-queue", action="store_true")
    parser.add_argument("--queue-base-as", choices=("user", "bot"), default="bot")
    parser.add_argument("--queue-base-profile", default="tax-risk-notifier")
    args = parser.parse_args()
    if len(args.fiscal_year) != 4 or not args.fiscal_year.isdigit():
        raise ValueError("fiscal year must contain four digits")

    started = monotonic()
    generated_at = datetime.now(UTC)
    companies, base_count, excluded_count = full_validation._load_base_records()
    print(
        f"company scope: {len(companies)} included, {excluded_count} blank codes excluded",
        flush=True,
    )
    settings = Settings(  # type: ignore[call-arg]
        _env_file=REPO_ROOT / "infra" / ".env"
    )
    sources = {
        SETTLEMENT_SOURCE: DgcSapProfitClient(
            full_validation._client_config(
                settings,
                url_name="dgc_sap_dividend_detail_api_url",
                key_name="dgc_sap_dividend_detail_app_key",
                secret_name="dgc_sap_dividend_detail_app_secret",
                page_size=settings.dgc_sap_dividend_detail_page_size,
            )
        ),
        TRIAL_BALANCE_SOURCE: DgcSapProfitClient(
            full_validation._client_config(
                settings,
                url_name="dgc_sap_trial_balance_api_url",
                key_name="dgc_sap_trial_balance_app_key",
                secret_name="dgc_sap_trial_balance_app_secret",
                page_size=settings.dgc_sap_trial_balance_page_size,
            )
        ),
        SAP_INCOME_SOURCE: DgcSapProfitClient(
            full_validation._client_config(
                settings,
                url_name="dgc_sap_profit_api_url",
                key_name="dgc_app_key",
                secret_name="dgc_app_secret",
                page_size=settings.dgc_page_size,
            )
        ),
    }
    coordinator = ParallelFetchCoordinator(
        sources,
        MemoryFetchCache(),
        FetchCoordinatorConfig(
            max_workers=settings.external_fetch_max_workers,
            source_concurrency={
                name: settings.external_fetch_source_concurrency.get(name, 4)
                for name in sources
            },
            cache_ttl_seconds=settings.external_fetch_cache_ttl_seconds,
            empty_cache_ttl_seconds=settings.external_fetch_empty_cache_ttl_seconds,
            lock_ttl_seconds=settings.external_fetch_lock_ttl_seconds,
            lock_wait_seconds=settings.external_fetch_lock_wait_seconds,
            lock_poll_seconds=settings.external_fetch_lock_poll_seconds,
            retry_max_attempts=settings.external_fetch_retry_max_attempts,
            retry_base_delay_seconds=settings.external_fetch_retry_base_delay_seconds,
            retry_max_delay_seconds=settings.external_fetch_retry_max_delay_seconds,
            retry_jitter_ratio=settings.external_fetch_retry_jitter_ratio,
        ),
    )

    settlement_requests = {
        company.code: FetchRequest(
            source_name=SETTLEMENT_SOURCE,
            parameters={"company": company.code, "fiscal_year": args.fiscal_year},
            schema_version="tax-adjustment-full-v1",
        )
        for company in companies
    }
    raw_settlement, settlement_errors = _fetch_many(
        coordinator,
        settlement_requests,
        max_workers=settings.external_fetch_max_workers,
        label="settlement",
    )

    settlement_by_company: dict[str, tuple[SettlementAdjustmentRow, ...]] = {}
    candidate_companies: list[dict[str, object]] = []
    candidate_company_payloads: dict[str, dict[str, object]] = {}
    summaries: dict[str, dict[AdjustmentSubject, dict[str, object]]] = {}
    for sequence, company in enumerate(companies, start=1):
        company_error = settlement_errors.get(company.code, "")
        rows: tuple[SettlementAdjustmentRow, ...] = ()
        if not company_error:
            try:
                rows = _settlement_rows(
                    raw_settlement[company.code],
                    company_code=company.code,
                    fiscal_year=args.fiscal_year,
                )
                settlement_by_company[company.code] = rows
            except Exception as error:
                company_error = _error_code(error)
                settlement_errors[company.code] = company_error

        subject_summaries: dict[AdjustmentSubject, dict[str, object]] = {}
        subject_rows: dict[str, list[dict[str, object]]] = {}
        for subject in AdjustmentSubject:
            relevant = tuple(
                row
                for row in rows
                if 1 <= int(row.fiscal_period) <= args.through_month
                and account_is_in_scope(subject, row.gl_account)
            )
            abnormal_count = sum(
                classify_detail(subject, row.detail_text).status is CheckStatus.ABNORMAL
                for row in relevant
            )
            subject_summaries[subject] = {
                "relevant_count": len(relevant),
                "abnormal_candidate_count": abnormal_count,
                "amount_ksl": format(
                    sum((row.amount_ksl for row in relevant), Decimal("0")),
                    "f",
                ),
            }
            subject_rows[subject.value] = [_json_row(row) for row in relevant]
        summaries[company.code] = subject_summaries
        company_payload = {
            "sequence": sequence,
            "company_code": company.code,
            "company_name": company.name,
            "source_row_count": len(rows),
            "status": "ERROR" if company_error else "OK",
            "error": company_error,
            "summary": {
                subject.value: subject_summaries[subject]
                for subject in AdjustmentSubject
            },
            "rows": subject_rows,
            "business_entertainment_candidates": [],
        }
        candidate_companies.append(company_payload)
        candidate_company_payloads[company.code] = company_payload

    welfare_companies = [
        company
        for company in companies
        if not settlement_errors.get(company.code)
        and _int_value(
            summaries[company.code][AdjustmentSubject.WELFARE]["abnormal_candidate_count"]
        )
        > 0
    ]
    donation_companies = [
        company
        for company in companies
        if not settlement_errors.get(company.code)
        and _int_value(
            summaries[company.code][AdjustmentSubject.DONATION]["abnormal_candidate_count"]
        )
        > 0
    ]
    business_companies = [
        company
        for company in companies
        if not settlement_errors.get(company.code)
        and any(
            1 <= int(row.fiscal_period) <= args.through_month
            and business_entertainment_account_is_in_scope(row.gl_account)
            for row in settlement_by_company[company.code]
        )
    ]
    business_evidence_companies = [
        company
        for company in business_companies
        if any(
            1 <= int(row.fiscal_period) <= args.through_month
            and business_entertainment_account_is_in_scope(row.gl_account)
            and extract_hesi_document_code(row.original_system_doc_no) is not None
            for row in settlement_by_company[company.code]
        )
    ]
    print(
        f"candidate gate: {len(welfare_companies)} welfare companies, "
        f"{len(donation_companies)} donation companies, "
        f"{len(business_companies)} business-entertainment companies "
        f"({len(business_evidence_companies)} require Hesi evidence)",
        flush=True,
    )

    trial_requests = {
        f"{company.code}:{month:03d}": FetchRequest(
            source_name=TRIAL_BALANCE_SOURCE,
            parameters={
                "company_code": company.code,
                "fiscal_year": args.fiscal_year,
                "fiscal_period": f"{month:03d}",
            },
            schema_version="tax-adjustment-salary-v1",
        )
        for company in welfare_companies
        for month in range(1, args.through_month + 1)
    }
    income_requests = {
        company.code: FetchRequest(
            source_name=SAP_INCOME_SOURCE,
            parameters={
                "bukrs": company.code,
                "gjahr": args.fiscal_year,
                "monat": f"{args.through_month:02d}",
            },
            schema_version="tax-adjustment-profit-v1",
        )
        for company in donation_companies
    }
    raw_trial, trial_errors = _fetch_many(
        coordinator,
        trial_requests,
        max_workers=settings.external_fetch_max_workers,
        label="salary-base",
    )
    raw_income, income_errors = _fetch_many(
        coordinator,
        income_requests,
        max_workers=settings.external_fetch_max_workers,
        label="profit-base",
    )
    hesi_company_codes = tuple(company.code for company in business_evidence_companies)
    hesi_detail_client: HesiDetailClient | None = None
    hesi_invoice_client: HesiInvoiceClient | None = None
    if (
        settings.dgc_hesi_reimbursement_enabled
        and settings.dgc_hesi_reimbursement_api_url is not None
        and settings.dgc_hesi_reimbursement_app_key is not None
        and settings.dgc_hesi_reimbursement_app_secret is not None
    ):
        detail_dgc_config = full_validation._client_config(
            settings,
            url_name="dgc_hesi_reimbursement_api_url",
            key_name="dgc_hesi_reimbursement_app_key",
            secret_name="dgc_hesi_reimbursement_app_secret",
            page_size=settings.dgc_hesi_reimbursement_page_size,
        )
        hesi_detail_client = HesiDetailClient(
            HesiDetailClientConfiguration(
                endpoint=settings.dgc_hesi_reimbursement_api_url,
                app_key=settings.dgc_hesi_reimbursement_app_key,
                app_secret=settings.dgc_hesi_reimbursement_app_secret,
                page_size=settings.dgc_hesi_reimbursement_page_size,
                max_records=settings.dgc_max_records,
                max_pages=settings.dgc_max_pages,
                max_page_bytes=settings.dgc_max_page_bytes,
                timeout_seconds=settings.dgc_timeout_seconds,
                tls_server_name=settings.dgc_tls_server_name,
            ),
            verify=_pinned_tls_context(detail_dgc_config) or True,
        )
    if (
        settings.dgc_hesi_invoice_enabled
        and settings.dgc_hesi_invoice_api_url is not None
        and settings.dgc_hesi_invoice_app_key is not None
        and settings.dgc_hesi_invoice_app_secret is not None
    ):
        invoice_dgc_config = full_validation._client_config(
            settings,
            url_name="dgc_hesi_invoice_api_url",
            key_name="dgc_hesi_invoice_app_key",
            secret_name="dgc_hesi_invoice_app_secret",
            page_size=settings.dgc_hesi_invoice_page_size,
            request_method="GET",
        )
        hesi_invoice_client = HesiInvoiceClient(
            HesiInvoiceClientConfiguration(
                endpoint=settings.dgc_hesi_invoice_api_url,
                app_key=settings.dgc_hesi_invoice_app_key,
                app_secret=settings.dgc_hesi_invoice_app_secret,
                page_size=settings.dgc_hesi_invoice_page_size,
                max_records=settings.dgc_max_records,
                max_pages=settings.dgc_max_pages,
                max_page_bytes=settings.dgc_max_page_bytes,
                timeout_seconds=settings.dgc_timeout_seconds,
                tls_server_name=settings.dgc_tls_server_name,
            ),
            verify=_pinned_tls_context(invoice_dgc_config) or True,
        )
    try:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="business-hesi") as pool:
            detail_future = pool.submit(
                _fetch_company_rows,
                hesi_detail_client.fetch_rows if hesi_detail_client else None,
                hesi_company_codes,
                max_workers=settings.external_fetch_source_concurrency.get(
                    HESI_DETAIL_SOURCE,
                    4,
                ),
                label="business-hesi-detail",
                unavailable_error="HESI_DETAIL_SOURCE_NOT_CONFIGURED",
            )
            invoice_future = pool.submit(
                _fetch_company_rows,
                hesi_invoice_client.fetch_rows if hesi_invoice_client else None,
                hesi_company_codes,
                max_workers=settings.external_fetch_source_concurrency.get(
                    HESI_INVOICE_SOURCE,
                    4,
                ),
                label="business-hesi-invoice",
                unavailable_error="HESI_INVOICE_SOURCE_NOT_CONFIGURED",
            )
            hesi_details_by_company, hesi_detail_errors = detail_future.result()
            hesi_invoices_by_company, hesi_invoice_errors = invoice_future.result()
    finally:
        if hesi_detail_client is not None:
            hesi_detail_client.close()
        if hesi_invoice_client is not None:
            hesi_invoice_client.close()

    application_company_codes: list[str] = []
    for company in business_evidence_companies:
        if company.code in hesi_detail_errors or company.code in hesi_invoice_errors:
            continue
        invoice_index: dict[str, list[HesiInvoiceRow]] = {}
        for invoice in hesi_invoices_by_company.get(company.code, ()):
            invoice_index.setdefault(invoice.code, []).append(invoice)
        needs_application = False
        for row in settlement_by_company[company.code]:
            if not (
                1 <= int(row.fiscal_period) <= args.through_month
                and business_entertainment_account_is_in_scope(row.gl_account)
            ):
                continue
            document_code = extract_hesi_document_code(row.original_system_doc_no)
            if document_code is None:
                continue
            if any(
                invoice.reception_apply_code
                for invoice in invoice_index.get(document_code, ())
            ):
                needs_application = True
                break
        if needs_application:
            application_company_codes.append(company.code)

    application_client: HesiApplicationClient | None = None
    if (
        settings.dgc_hesi_application_enabled
        and settings.dgc_hesi_application_api_url is not None
        and settings.dgc_hesi_application_app_key is not None
        and settings.dgc_hesi_application_app_secret is not None
    ):
        application_dgc_config = full_validation._client_config(
            settings,
            url_name="dgc_hesi_application_api_url",
            key_name="dgc_hesi_application_app_key",
            secret_name="dgc_hesi_application_app_secret",
            page_size=settings.dgc_hesi_application_page_size,
        )
        application_client = HesiApplicationClient(
            HesiApplicationClientConfiguration(
                endpoint=settings.dgc_hesi_application_api_url,
                app_key=settings.dgc_hesi_application_app_key,
                app_secret=settings.dgc_hesi_application_app_secret,
                page_size=settings.dgc_hesi_application_page_size,
                max_records=settings.dgc_max_records,
                max_pages=settings.dgc_max_pages,
                max_page_bytes=settings.dgc_max_page_bytes,
                timeout_seconds=settings.dgc_timeout_seconds,
                tls_server_name=settings.dgc_tls_server_name,
            ),
            verify=_pinned_tls_context(application_dgc_config) or True,
        )
    try:
        applications_by_company, application_errors = _fetch_company_rows(
            application_client.fetch_rows if application_client else None,
            tuple(application_company_codes),
            max_workers=settings.external_fetch_source_concurrency.get(
                "dgc_hesi_application",
                4,
            ),
            label="business-hesi-application",
            unavailable_error="HESI_APPLICATION_SOURCE_NOT_CONFIGURED",
        )
    finally:
        if application_client is not None:
            application_client.close()
    print(
        f"business evidence gate: {len(application_company_codes)} companies "
        "require Hesi applications",
        flush=True,
    )

    business_service = BusinessEntertainmentAccountCheckService(
        settlement_source=_SettlementMapSource(settlement_by_company),
        hesi_detail_source=_HesiDetailMapSource(
            hesi_details_by_company,
            hesi_detail_errors,
        ),
        hesi_invoice_source=_HesiInvoiceMapSource(
            hesi_invoices_by_company,
            hesi_invoice_errors,
        ),
        hesi_application_source=_HesiApplicationMapSource(
            applications_by_company,
            application_errors,
        ),
    )

    result_rows: list[dict[str, object]] = []
    alert_details: list[dict[str, object]] = []
    checker = TaxAdjustmentAccountCheckService(source=object())  # type: ignore[arg-type]
    for sequence, company in enumerate(companies, start=1):
        subject_summary = summaries[company.code]
        welfare_summary = subject_summary[AdjustmentSubject.WELFARE]
        donation_summary = subject_summary[AdjustmentSubject.DONATION]
        company_settlement_rows = settlement_by_company.get(company.code, ())
        business_rows = tuple(
            row
            for row in company_settlement_rows
            if 1 <= int(row.fiscal_period) <= args.through_month
            and business_entertainment_account_is_in_scope(row.gl_account)
        )
        result = _result_template(
            sequence=sequence,
            company_code=company.code,
            company_name=company.name,
            fiscal_year=args.fiscal_year,
            through_month=args.through_month,
            source_row_count=len(settlement_by_company.get(company.code, ())),
            welfare_cumulative=Decimal(str(welfare_summary["amount_ksl"])),
            donation_cumulative=Decimal(str(donation_summary["amount_ksl"])),
            welfare_candidate_count=_int_value(
                welfare_summary["abnormal_candidate_count"]
            ),
            donation_candidate_count=_int_value(
                donation_summary["abnormal_candidate_count"]
            ),
            business_entertainment_cumulative=sum(
                (row.amount_ksl for row in business_rows),
                Decimal("0"),
            ),
            business_entertainment_detail_count=len(business_rows),
        )
        source_error = settlement_errors.get(company.code)
        if source_error:
            result.update(
                {
                    "welfare_status": "ERROR",
                    "welfare_reason": "SOURCE_ERROR",
                    "welfare_error": source_error,
                    "donation_status": "ERROR",
                    "donation_reason": "SOURCE_ERROR",
                    "donation_error": source_error,
                    "business_entertainment_status": "ERROR",
                    "business_entertainment_reason": "SOURCE_ERROR",
                    "business_entertainment_evidence_status": "BLOCKED",
                    "business_entertainment_error": source_error,
                }
            )
            result_rows.append(result)
            continue

        settlement_rows = settlement_by_company[company.code]
        welfare_candidate_count = _int_value(
            welfare_summary["abnormal_candidate_count"]
        )
        if welfare_candidate_count:
            try:
                monthly_rows: dict[int, tuple[TrialBalanceRow, ...]] = {}
                for month in range(1, args.through_month + 1):
                    key = f"{company.code}:{month:03d}"
                    if key in trial_errors:
                        raise RuntimeError(trial_errors[key])
                    monthly_rows[month] = _trial_balance_rows(
                        raw_trial[key],
                        company_code=company.code,
                        fiscal_year=args.fiscal_year,
                        fiscal_period=f"{month:03d}",
                    )
                request = AccountCheckRequest(
                    subject=AdjustmentSubject.WELFARE,
                    company=company.code,
                    fiscal_year=args.fiscal_year,
                    through_month=args.through_month,
                )
                welfare_adjustment = calculate_welfare_adjustment(
                    request,
                    settlement_rows=settlement_rows,
                    trial_balance_rows_by_month=monthly_rows,
                )
                final = welfare_adjustment.monthly_summaries[-1]
                checked = checker.check_rows(
                    request,
                    source_rows=settlement_rows,
                    adjustment_amount=welfare_adjustment.adjustment_amount,
                )
                abnormal = tuple(
                    detail for detail in checked.details if detail.status is CheckStatus.ABNORMAL
                )
                result.update(
                    {
                        "welfare_status": "ALERT" if abnormal else "CLEAR",
                        "welfare_reason": (
                            "ADJUSTMENT_AND_ABNORMAL_DETAILS"
                            if abnormal
                            else "ADJUSTMENT_GATE_NOT_MET"
                        ),
                        "salary_cumulative": format(final.cumulative_salary_amount, "f"),
                        "welfare_deduction_limit": format(final.deduction_limit, "f"),
                        "welfare_adjustment": format(
                            welfare_adjustment.adjustment_amount, "f"
                        ),
                        "welfare_detail_selected": str(
                            welfare_adjustment.detail_check_selected
                        ).lower(),
                        "welfare_alert_count": len(abnormal),
                        "welfare_alert_amount": format(
                            sum((detail.row.amount_ksl for detail in abnormal), Decimal("0")),
                            "f",
                        ),
                    }
                )
                for detail in abnormal:
                    alert_details.append(
                        _alert_detail(
                            sequence=len(alert_details) + 1,
                            company_name=company.name,
                            subject=AdjustmentSubject.WELFARE,
                            row=detail.row,
                        )
                    )
            except Exception as error:
                result.update(
                    {
                        "welfare_status": "ERROR",
                        "welfare_reason": "FORMULA_SOURCE_ERROR",
                        "welfare_error": _error_code(error),
                    }
                )

        donation_candidate_count = _int_value(
            donation_summary["abnormal_candidate_count"]
        )
        if donation_candidate_count:
            try:
                if company.code in income_errors:
                    raise RuntimeError(income_errors[company.code])
                income_rows = _income_rows(
                    raw_income[company.code],
                    company_code=company.code,
                    fiscal_year=args.fiscal_year,
                    fiscal_period=f"{args.through_month:02d}",
                )
                request = AccountCheckRequest(
                    subject=AdjustmentSubject.DONATION,
                    company=company.code,
                    fiscal_year=args.fiscal_year,
                    through_month=args.through_month,
                )
                donation_adjustment = calculate_donation_adjustment(
                    request,
                    settlement_rows=settlement_rows,
                    sap_income_rows=income_rows,
                )
                checked = checker.check_rows(
                    request,
                    source_rows=settlement_rows,
                    adjustment_amount=donation_adjustment.adjustment_amount,
                )
                abnormal = tuple(
                    detail for detail in checked.details if detail.status is CheckStatus.ABNORMAL
                )
                result.update(
                    {
                        "donation_status": "ALERT" if abnormal else "CLEAR",
                        "donation_reason": (
                            "ADJUSTMENT_AND_ABNORMAL_DETAILS"
                            if abnormal
                            else "ADJUSTMENT_GATE_NOT_MET"
                        ),
                        "donation_profit_cumulative": format(
                            donation_adjustment.cumulative_profit_amount, "f"
                        ),
                        "donation_deduction_limit": format(
                            donation_adjustment.deduction_limit, "f"
                        ),
                        "donation_adjustment": format(
                            donation_adjustment.adjustment_amount, "f"
                        ),
                        "donation_detail_selected": str(
                            donation_adjustment.detail_check_selected
                        ).lower(),
                        "donation_alert_count": len(abnormal),
                    }
                )
                for detail in abnormal:
                    alert_details.append(
                        _alert_detail(
                            sequence=len(alert_details) + 1,
                            company_name=company.name,
                            subject=AdjustmentSubject.DONATION,
                            row=detail.row,
                        )
                    )
            except Exception as error:
                result.update(
                    {
                        "donation_status": "ERROR",
                        "donation_reason": "FORMULA_SOURCE_ERROR",
                        "donation_error": _error_code(error),
                    }
                )

        if business_rows:
            try:
                business_check = business_service.run(
                    BusinessEntertainmentCheckRequest(
                        company=company.code,
                        fiscal_year=args.fiscal_year,
                        through_month=args.through_month,
                    )
                )
                business_abnormal = tuple(
                    detail
                    for detail in business_check.details
                    if detail.status is CheckStatus.ABNORMAL
                )
                business_candidates = [
                    _business_candidate(
                        sequence=index,
                        company_name=company.name,
                        detail=detail,
                    )
                    for index, detail in enumerate(business_abnormal, start=1)
                ]
                candidate_company_payloads[company.code][
                    "business_entertainment_candidates"
                ] = business_candidates
                result.update(
                    {
                        "business_entertainment_status": (
                            "ALERT" if business_abnormal else "CLEAR"
                        ),
                        "business_entertainment_reason": (
                            "ABNORMAL_DETAILS_FOUND"
                            if business_abnormal
                            else "EVIDENCE_CHAIN_CLEAR"
                        ),
                        "business_entertainment_alert_count": len(business_abnormal),
                        "business_entertainment_alert_amount": format(
                            sum(
                                (detail.row.amount_ksl for detail in business_abnormal),
                                Decimal("0"),
                            ),
                            "f",
                        ),
                        "business_entertainment_hesi_detail_count": (
                            business_check.hesi_detail_source_row_count
                        ),
                        "business_entertainment_hesi_invoice_count": (
                            business_check.hesi_invoice_source_row_count
                        ),
                        "business_entertainment_hesi_application_count": (
                            business_check.hesi_application_source_row_count
                        ),
                        "business_entertainment_evidence_status": "COMPLETE",
                    }
                )
                alert_details.extend(business_candidates)
            except Exception as error:
                result.update(
                    {
                        "business_entertainment_status": "ERROR",
                        "business_entertainment_reason": "EVIDENCE_SOURCE_ERROR",
                        "business_entertainment_evidence_status": "BLOCKED",
                        "business_entertainment_error": _error_code(error),
                    }
                )
        result_rows.append(result)

    counts = {
        "companies": len(companies),
        "source_ok": len(companies) - len(settlement_errors),
        "source_error": len(settlement_errors),
        "welfare_alert": sum(row["welfare_status"] == "ALERT" for row in result_rows),
        "welfare_clear": sum(row["welfare_status"] == "CLEAR" for row in result_rows),
        "welfare_error": sum(row["welfare_status"] == "ERROR" for row in result_rows),
        "donation_alert": sum(row["donation_status"] == "ALERT" for row in result_rows),
        "donation_clear": sum(row["donation_status"] == "CLEAR" for row in result_rows),
        "donation_error": sum(row["donation_status"] == "ERROR" for row in result_rows),
        "business_entertainment_alert": sum(
            row["business_entertainment_status"] == "ALERT" for row in result_rows
        ),
        "business_entertainment_clear": sum(
            row["business_entertainment_status"] == "CLEAR" for row in result_rows
        ),
        "business_entertainment_error": sum(
            row["business_entertainment_status"] == "ERROR" for row in result_rows
        ),
        "alert_details": len(alert_details),
    }
    scope = {
        "company_count": len(companies),
        "base_record_count": base_count,
        "excluded_blank_company_count": excluded_count,
        "fiscal_year": args.fiscal_year,
        "through_month": args.through_month,
        "filter": "nonblank company codes from the latest Lark Base data",
    }
    output_dir = args.output_dir.resolve()
    stem = f"{args.fiscal_year}_{args.through_month:02d}"
    result_path = output_dir / f"tax_adjustment_accounts_full_{stem}.json"
    candidate_path = output_dir / f"tax_adjustment_accounts_candidates_{stem}.json"
    result_csv_path = output_dir / f"tax_adjustment_accounts_full_{stem}.csv"
    alert_csv_path = output_dir / f"tax_adjustment_accounts_alert_details_{stem}.csv"
    _atomic_json(
        candidate_path,
        {
            "schema_version": 1,
            "generated_at": generated_at.isoformat(),
            "scope": scope,
            "elapsed_seconds": round(monotonic() - started, 3),
            "completed": True,
            "counts": {
                "ok": counts["source_ok"],
                "error": counts["source_error"],
                "welfare_candidates": sum(
                    _int_value(row["welfare_abnormal_candidate_count"])
                    for row in result_rows
                ),
                "donation_candidates": sum(
                    _int_value(row["donation_abnormal_candidate_count"])
                    for row in result_rows
                ),
                "business_entertainment_candidates": sum(
                    _int_value(row["business_entertainment_alert_count"])
                    for row in result_rows
                ),
            },
            "companies": candidate_companies,
        },
    )
    _atomic_json(
        result_path,
        {
            "schema_version": 1,
            "generated_at": generated_at.isoformat(),
            "scope": scope,
            "method": {
                "all_companies": "settlement detail scan",
                "candidate_gate": (
                    "only companies with abnormal classified details require base calculation"
                ),
                "welfare_limit_rate": "0.14",
                "donation_limit_rate": "0.12",
                "business_entertainment_evidence_chain": (
                    "settlement detail -> Hesi detail -> Hesi invoice -> Hesi application"
                ),
            },
            "elapsed_seconds": round(monotonic() - started, 3),
            "counts": counts,
            "rows": result_rows,
            "alert_details": alert_details,
        },
    )
    _write_csv(result_csv_path, result_rows)
    _write_csv(alert_csv_path, alert_details)

    web_report_path = args.web_report.resolve()
    merged_report: dict[str, object] | None = None
    if web_report_path.exists():
        report = json.loads(web_report_path.read_text(encoding="utf-8"))
        merged = merge_tax_adjustment_report(
            report,
            result_path=result_path,
            candidate_path=candidate_path,
        )
        merged["generated_at"] = datetime.now(UTC).isoformat()
        _atomic_json(web_report_path, merged)
        merged_report = merged
        print(f"web report updated: {web_report_path}", flush=True)

    print(json.dumps(counts, ensure_ascii=False), flush=True)
    print(f"result written: {result_path}", flush=True)
    print(f"candidate evidence written: {candidate_path}", flush=True)
    coordinator.close()
    for source in sources.values():
        source.close()
    if application_client is not None:
        application_client.close()
    if merged_report is None:
        print("alert archive skipped: web report does not exist", flush=True)
    elif args.skip_alert_archive:
        print("alert archive skipped by explicit option", flush=True)
    else:
        _, archive_results = archive_report(
            merged_report,
            monitor_codes={"tax_adjustment_account_accuracy"},
            base_identity=args.archive_base_as,
            base_profile=args.archive_base_profile,
        )
        for archive_result in archive_results:
            print(
                f"alert archive {archive_result.table_name}: "
                f"created={archive_result.created_rows}, "
                f"restored={archive_result.restored_rows}, "
                f"retired={archive_result.retired_rows}",
                flush=True,
            )
    if merged_report is None:
        print("alert queue skipped: web report does not exist", flush=True)
    elif args.skip_alert_queue:
        print("alert queue skipped by explicit option", flush=True)
    else:
        queue_plan, queue_result = enqueue_report(
            merged_report,
            monitor_codes={"tax_adjustment_account_accuracy"},
            base_identity=args.queue_base_as,
            base_profile=args.queue_base_profile,
        )
        assert queue_result is not None
        print(
            "alert queue tax_adjustment_account_accuracy: "
            f"planned={len(queue_plan.items)}, "
            f"created={queue_result.created_rows}, "
            f"existing={queue_result.existing_rows}",
            flush=True,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
