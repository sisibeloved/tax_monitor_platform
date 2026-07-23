from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from copy import deepcopy
import json
from pathlib import Path
from typing import Final

from tax_risk.application.tax_adjustment_accounts.contracts import (
    AdjustmentLabel,
    AdjustmentSubject,
    CheckStatus,
    SettlementAdjustmentRow,
)
from tax_risk.application.tax_adjustment_accounts.rules import (
    classify_detail,
    recommended_accounts,
)


MONITOR_CODE: Final[str] = "tax_adjustment_account_accuracy"
MONITOR_NAME: Final[str] = "纳税调增科目准确性检查"
EVIDENCE_NOTICE: Final[str] = (
    "本期真实结果覆盖福利费及公益性捐赠科目；业务招待费仍沿原语义监控链路单独运行。"
    "疑似错入明细仅在纳税调增额大于0时示警；未达到门槛的候选明细仍可展开查看和导出。"
    "建议入账科目为规则判断的科目名称，具体SAP科目编码需按公司适用科目表确认。"
)

_LABEL_NAMES: Final[Mapping[AdjustmentLabel, str]] = {
    AdjustmentLabel.WELFARE_REASONABLE: "福利费入账合理",
    AdjustmentLabel.WELFARE_BUSINESS_ENTERTAINMENT: "业务招待费异常",
    AdjustmentLabel.WELFARE_EMPLOYEE_EDUCATION: "职工教育经费异常",
    AdjustmentLabel.WELFARE_ADVERTISING_PROMOTION: "广告宣传费异常",
    AdjustmentLabel.WELFARE_CUSTOMER_GIFT_REVIEW: "广告宣传或业务招待待复核",
    AdjustmentLabel.DONATION_REASONABLE: "公益性捐赠入账合理",
    AdjustmentLabel.DONATION_SPONSORSHIP: "赞助支出异常",
    AdjustmentLabel.DONATION_ADVERTISING_PROMOTION: "广告宣传异常",
}


class TaxAdjustmentWebReportError(ValueError):
    pass


def merge_tax_adjustment_report(
    report: Mapping[str, object],
    *,
    result_path: Path,
    candidate_path: Path | None = None,
) -> dict[str, object]:
    merged = deepcopy(dict(report))
    fiscal_year = _integer(merged.get("fiscal_year"), "report fiscal_year")
    through_period = _integer(merged.get("through_period"), "report through_period")
    companies = _object_list(merged.get("companies"), "report companies")
    company_codes = tuple(_company_code(company) for company in companies)
    if len(set(company_codes)) != len(company_codes):
        raise TaxAdjustmentWebReportError("report contains duplicate company codes")

    if result_path.exists():
        result_rows = _load_result_rows(
            result_path,
            fiscal_year=fiscal_year,
            through_period=through_period,
            company_codes=company_codes,
        )
        candidate_rows = _load_candidate_rows(
            candidate_path,
            fiscal_year=fiscal_year,
            through_period=through_period,
        )
        monitor_results = {
            code: _monitor_result(result_rows[code], candidate_rows.get(code))
            for code in company_codes
        }
        runtime_status = "DATA"
    else:
        monitor_results = {
            code: {
                "status": "BLOCKED",
                "outcome": "无法计算",
                "reason": "纳税调增科目全量结果文件不存在",
                "evidence_limited": True,
                "values": {},
                "candidates": [],
            }
            for code in company_codes
        }
        runtime_status = "MISSING"

    for company in companies:
        code = _company_code(company)
        raw_results = company.get("monitor_results")
        if not isinstance(raw_results, dict):
            raise TaxAdjustmentWebReportError(f"company {code} monitor_results must be an object")
        raw_results[MONITOR_CODE] = monitor_results[code]

    counts = Counter(str(result["status"]) for result in monitor_results.values())
    raw_summary = merged.get("monitor_summary")
    if not isinstance(raw_summary, dict):
        raise TaxAdjustmentWebReportError("report monitor_summary must be an object")
    raw_summary[MONITOR_CODE] = {
        "name": MONITOR_NAME,
        "total": len(companies),
        "ALERT": counts["ALERT"],
        "CLEAR": counts["CLEAR"],
        "BLOCKED": counts["BLOCKED"],
        "NOT_APPLICABLE": counts["NOT_APPLICABLE"],
    }
    merged["tax_adjustment_account_accuracy_notice"] = EVIDENCE_NOTICE

    raw_runtime = merged.get("runtime")
    if not isinstance(raw_runtime, dict):
        raise TaxAdjustmentWebReportError("report runtime must be an object")
    raw_runtime[MONITOR_CODE] = {
        "status": runtime_status,
        "company_count": len(companies),
        "source_error_count": counts["BLOCKED"],
        "candidate_company_count": sum(
            1 for result in monitor_results.values() if _candidate_list(result)
        ),
        "candidate_detail_count": sum(
            len(_candidate_list(result)) for result in monitor_results.values()
        ),
        "formula_evaluated_company_count": sum(
            1
            for result in monitor_results.values()
            if _positive_count(_result_values(result).get("welfare_abnormal_candidate_count"))
            or _positive_count(_result_values(result).get("donation_abnormal_candidate_count"))
        ),
    }
    return merged


def _load_result_rows(
    path: Path,
    *,
    fiscal_year: int,
    through_period: int,
    company_codes: tuple[str, ...],
) -> dict[str, dict[str, object]]:
    payload = _json_object(path)
    scope = payload.get("scope")
    if not isinstance(scope, dict):
        raise TaxAdjustmentWebReportError("tax-adjustment result scope must be an object")
    _validate_period(scope, fiscal_year=fiscal_year, through_period=through_period)
    rows = _object_list(payload.get("rows"), "tax-adjustment result rows")
    indexed: dict[str, dict[str, object]] = {}
    for row in rows:
        code = _company_code(row)
        if code in indexed:
            raise TaxAdjustmentWebReportError(f"duplicate tax-adjustment company {code}")
        indexed[code] = row
    missing = sorted(set(company_codes) - set(indexed))
    if missing:
        raise TaxAdjustmentWebReportError(
            f"tax-adjustment result is missing companies: {','.join(missing)}"
        )
    return indexed


def _load_candidate_rows(
    path: Path | None,
    *,
    fiscal_year: int,
    through_period: int,
) -> dict[str, tuple[dict[str, str], ...]]:
    if path is None or not path.exists():
        return {}
    payload = _json_object(path)
    scope = payload.get("scope")
    if not isinstance(scope, dict):
        raise TaxAdjustmentWebReportError("tax-adjustment candidate scope must be an object")
    _validate_period(scope, fiscal_year=fiscal_year, through_period=through_period)
    companies = _object_list(payload.get("companies"), "tax-adjustment candidate companies")
    result: dict[str, tuple[dict[str, str], ...]] = {}
    for company in companies:
        code = _company_code(company)
        raw_subjects = company.get("rows")
        if not isinstance(raw_subjects, dict):
            raise TaxAdjustmentWebReportError(f"candidate rows for {code} must be an object")
        candidates: list[dict[str, str]] = []
        for subject in AdjustmentSubject:
            raw_rows = raw_subjects.get(subject.value, [])
            if not isinstance(raw_rows, list):
                raise TaxAdjustmentWebReportError(
                    f"candidate {subject.value} rows for {code} must be a list"
                )
            for raw_row in raw_rows:
                try:
                    row = SettlementAdjustmentRow.model_validate(raw_row)
                except ValueError as error:
                    raise TaxAdjustmentWebReportError(
                        f"candidate row validation failed for {code}"
                    ) from error
                if row.company != code or row.fiscal_year != str(fiscal_year):
                    raise TaxAdjustmentWebReportError(
                        f"candidate row escaped company/year scope for {code}"
                    )
                decision = classify_detail(subject, row.detail_text)
                if decision.status is not CheckStatus.ABNORMAL:
                    continue
                candidates.append(
                    {
                        "candidate_no": str(len(candidates) + 1),
                        "subject": "福利费"
                        if subject is AdjustmentSubject.WELFARE
                        else "公益性捐赠",
                        "fiscal_period": row.fiscal_period,
                        "voucher_no": row.voucher_no,
                        "original_system_doc_no": row.original_system_doc_no,
                        "gl_account": row.gl_account,
                        "account_name": row.account_name,
                        "header_text": row.header_text,
                        "detail_text": row.detail_text,
                        "amount": format(row.amount_ksl, "f"),
                        "currency": row.group_currency,
                        "classification": "、".join(
                            _LABEL_NAMES[label] for label in decision.labels
                        ),
                        "matched_keywords": "、".join(decision.matched_keywords),
                        "recommended_account": "、".join(recommended_accounts(decision.labels)),
                        "recommendation_basis": (
                            f"行项目摘要命中关键词：{'、'.join(decision.matched_keywords)}"
                        ),
                    }
                )
        result[code] = tuple(candidates)
    return result


def _monitor_result(
    row: Mapping[str, object],
    candidates: tuple[dict[str, str], ...] | None,
) -> dict[str, object]:
    welfare_status = str(row.get("welfare_status") or "ERROR")
    donation_status = str(row.get("donation_status") or "ERROR")
    statuses = {welfare_status, donation_status}
    evidence_candidates = candidates or ()
    if candidates is None:
        welfare_candidate_count = _count(row.get("welfare_abnormal_candidate_count"))
        donation_candidate_count = _count(row.get("donation_abnormal_candidate_count"))
    else:
        welfare_candidate_count = sum(
            candidate.get("subject") == "福利费" for candidate in candidates
        )
        donation_candidate_count = sum(
            candidate.get("subject") == "公益性捐赠" for candidate in candidates
        )
    candidate_count = welfare_candidate_count + donation_candidate_count

    if "ERROR" in statuses:
        status = "BLOCKED"
        outcome = "无法计算"
        reason = (
            "；".join(
                str(value)
                for value in (row.get("welfare_error"), row.get("donation_error"))
                if value
            )
            or "纳税调增科目结果不可用"
        )
        alert_code = None
    elif "ALERT" in statuses:
        status = "ALERT"
        outcome = "发现疑似错入科目"
        reason = None
        alert_code = "TAX_ADJUSTMENT_ACCOUNT_MISCLASSIFIED"
    else:
        status = "CLEAR"
        outcome = "存在候选但未达到调增门槛" if candidate_count > 0 else "未发现需示警的错入科目"
        reason = (
            f"发现{candidate_count}条疑似错入候选，但纳税调增额为0，按门槛规则不示警"
            if candidate_count > 0
            else None
        )
        alert_code = None

    return {
        "status": status,
        "outcome": outcome,
        "reason": reason,
        "alert_code": alert_code,
        "evidence_limited": True,
        "values": {
            "welfare_cumulative": _optional_text(row.get("welfare_cumulative")),
            "salary_cumulative": _optional_text(row.get("salary_cumulative")),
            "welfare_deduction_limit": _optional_text(row.get("welfare_deduction_limit")),
            "welfare_adjustment": _optional_text(row.get("welfare_adjustment")),
            "welfare_detail_selected": _optional_text(row.get("welfare_detail_selected")),
            "welfare_abnormal_candidate_count": str(welfare_candidate_count),
            "welfare_alert_count": str(_count(row.get("welfare_alert_count"))),
            "welfare_alert_amount": _optional_text(row.get("welfare_alert_amount")),
            "donation_cumulative": _optional_text(row.get("donation_cumulative")),
            "donation_abnormal_candidate_count": str(donation_candidate_count),
            "donation_alert_count": str(_count(row.get("donation_alert_count"))),
        },
        "candidates": list(evidence_candidates),
    }


def _json_object(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise TaxAdjustmentWebReportError(f"cannot read {path}") from error
    if not isinstance(payload, dict):
        raise TaxAdjustmentWebReportError(f"{path} must contain a JSON object")
    return payload


def _validate_period(scope: Mapping[str, object], *, fiscal_year: int, through_period: int) -> None:
    if _integer(scope.get("fiscal_year"), "scope fiscal_year") != fiscal_year:
        raise TaxAdjustmentWebReportError("tax-adjustment fiscal year does not match report")
    if _integer(scope.get("through_month"), "scope through_month") != through_period:
        raise TaxAdjustmentWebReportError("tax-adjustment period does not match report")


def _object_list(value: object, field: str) -> list[dict[str, object]]:
    if not isinstance(value, list) or not all(isinstance(item, dict) for item in value):
        raise TaxAdjustmentWebReportError(f"{field} must be a list of objects")
    return value


def _company_code(value: Mapping[str, object]) -> str:
    code = value.get("company_code")
    if not isinstance(code, str) or not code.strip():
        raise TaxAdjustmentWebReportError("company_code must be a non-empty string")
    return code.strip()


def _integer(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise TaxAdjustmentWebReportError(f"{field} must be an integer")
    if not isinstance(value, (int, str)):
        raise TaxAdjustmentWebReportError(f"{field} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as error:
        raise TaxAdjustmentWebReportError(f"{field} must be an integer") from error
    return parsed


def _count(value: object) -> int:
    parsed = _integer(value, "candidate count")
    if parsed < 0:
        raise TaxAdjustmentWebReportError("candidate count cannot be negative")
    return parsed


def _positive_count(value: object) -> bool:
    if value is None:
        return False
    try:
        return _integer(value, "candidate count") > 0
    except TaxAdjustmentWebReportError:
        return False


def _candidate_list(result: Mapping[str, object]) -> list[object]:
    candidates = result.get("candidates")
    return candidates if isinstance(candidates, list) else []


def _result_values(result: Mapping[str, object]) -> Mapping[str, object]:
    values = result.get("values")
    return values if isinstance(values, dict) else {}


def _optional_text(value: object) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


__all__ = [
    "EVIDENCE_NOTICE",
    "MONITOR_CODE",
    "MONITOR_NAME",
    "TaxAdjustmentWebReportError",
    "merge_tax_adjustment_report",
]
