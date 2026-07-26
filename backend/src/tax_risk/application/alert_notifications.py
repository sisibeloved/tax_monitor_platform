"""Plan manual Lark Base queue rows for six-monitor alert results."""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
from typing import Final


MONITORS: Final[tuple[tuple[str, str], ...]] = (
    ("current_tax_accrual", "季度应计提所得税准确性检查"),
    ("deferred_tax", "递延所得税计提/转回准确性检查"),
    ("refund", "所得税退税进度监控及入账科目准确性检查"),
    ("tax_burden", "当年累计税负率异常监测"),
    ("potential_tax_cost", "潜在纳税调增税务成本"),
    ("tax_adjustment_account_accuracy", "纳税调增科目准确性检查"),
)
MAX_ALERT_COMPANIES_PER_MONITOR: Final[int] = 3
MAX_CANDIDATE_LINES: Final[int] = 20
QUEUE_PROTOCOL_VERSION: Final[str] = "base-manual-v1"

_VALUE_FIELDS: Final[dict[str, tuple[tuple[str, str, str], ...]]] = {
    "current_tax_accrual": (
        ("cumulative_profit", "损益表累计利润总额", "money"),
        ("received_dividends", "累计分红", "money"),
        ("fair_value_change", "公允价值变动损益", "money"),
        ("loss_carryforward", "可弥补以前年度亏损", "money"),
        ("tax_rate", "所得税税率", "rate"),
        ("prior_quarter_current_tax", "上季度累计所得税费用", "money"),
        ("current_quarter_should_accrue", "本季度应计提所得税", "money"),
        ("current_quarter_current_tax", "本季度所得税费用发生额", "money"),
        ("difference", "计提差异", "money"),
    ),
    "deferred_tax": (
        ("cumulative_profit", "损益表累计利润总额", "money"),
        ("loss_carryforward", "可弥补以前年度亏损", "money"),
        ("deferred_tax_rate", "递延所得税税率", "rate"),
        ("deferred_tax_base", "递延所得税计税基础", "money"),
        ("sap_cumulative_deferred_tax_expense", "SAP累计已计提递延所得税费用", "money"),
        ("system_cumulative_deferred_tax", "系统测算累计递延所得税", "money"),
        ("adjustment", "本年应计提/转回金额", "money"),
    ),
    "refund": (
        ("refund_amount", "所得税退税金额", "money"),
        ("match_count", "等额候选数", "count"),
        ("booking_account", "入账科目代码", "text"),
        ("booking_account_family", "入账科目类别", "text"),
        ("match_stage", "匹配阶段", "text"),
        ("receipt_source", "到账判断来源", "text"),
    ),
    "tax_burden": (
        ("cumulative_tax_payable", "本年累计应纳税额", "money"),
        ("cumulative_revenue", "损益表累计营业收入", "money"),
        ("current_tax_burden", "本年累计所得税税负率", "rate"),
        ("historical_tax_burden", "历史税负率", "rate"),
        ("deviation", "税负率偏差", "rate"),
    ),
    "potential_tax_cost": (
        ("other_payables_accrual", "其他应付款暂估", "money"),
        ("reimbursement_expense_total", "合思报销费用总额", "money"),
        ("invoice_approved_total", "已取得发票金额", "money"),
        ("hesi_no_invoice", "合思无票报销金额", "money"),
        ("potential_adjustment", "潜在纳税调增金额", "money"),
        ("cumulative_tax_payable", "当前应纳税额", "money"),
        ("potential_tax_payable", "调增后潜在应纳税额", "money"),
        ("potential_tax_cost", "潜在税务成本", "money"),
    ),
    "tax_adjustment_account_accuracy": (
        ("business_entertainment_alert_count", "业务招待费示警明细数", "count"),
        ("business_entertainment_alert_amount", "业务招待费示警金额", "money"),
        ("welfare_alert_count", "福利费示警明细数", "count"),
        ("welfare_alert_amount", "福利费示警金额", "money"),
        ("donation_alert_count", "公益性捐赠示警明细数", "count"),
    ),
}


class AlertNotificationError(ValueError):
    """Raised when a report or Base relationship is unsafe to enqueue."""


@dataclass(frozen=True, slots=True)
class CompanyLink:
    record_id: str
    company_code: str
    company_name: str


@dataclass(frozen=True, slots=True)
class CompanyLinkDirectory:
    companies: Mapping[str, CompanyLink]
    issues: Mapping[str, str]
    source_record_count: int
    excluded_blank_company_count: int


@dataclass(frozen=True, slots=True)
class AlertSelection:
    monitor_code: str
    monitor_name: str
    company_code: str
    company_name: str
    result: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class AlertQueueItem:
    idempotency_key: str
    company_record_id: str
    company_code: str
    company_name: str
    monitor_code: str
    monitor_name: str
    period: str
    outcome: str
    key_values: str
    alert_details: str
    dashboard_url: str
    test_push: bool


@dataclass(frozen=True, slots=True)
class SkippedQueueItem:
    company_code: str
    company_name: str
    monitor_code: str
    reason_code: str
    reason: str


@dataclass(frozen=True, slots=True)
class AlertQueuePlan:
    selections: tuple[AlertSelection, ...]
    items: tuple[AlertQueueItem, ...]
    skipped: tuple[SkippedQueueItem, ...]


def select_alerts(
    report: Mapping[str, object],
    *,
    max_companies_per_monitor: int = MAX_ALERT_COMPANIES_PER_MONITOR,
    company_codes: Collection[str] | None = None,
    monitor_codes: Collection[str] | None = None,
) -> tuple[AlertSelection, ...]:
    """Select the first N ALERT companies per monitor in report order."""

    if type(max_companies_per_monitor) is not int or not (
        1 <= max_companies_per_monitor <= MAX_ALERT_COMPANIES_PER_MONITOR
    ):
        raise AlertNotificationError("max_companies_per_monitor must be between 1 and 3")
    companies = report.get("companies")
    if not isinstance(companies, list):
        raise AlertNotificationError("report.companies must be a list")
    monitor_summary = report.get("monitor_summary")
    if not isinstance(monitor_summary, Mapping):
        monitor_summary = {}
    normalized_company_codes = (
        {_required_text(code, "company_codes item") for code in company_codes}
        if company_codes is not None
        else None
    )
    selected_monitors = _requested_monitors(monitor_codes)

    selections: list[AlertSelection] = []
    for monitor_code, fallback_name in selected_monitors:
        summary = monitor_summary.get(monitor_code)
        monitor_name = fallback_name
        if isinstance(summary, Mapping) and isinstance(summary.get("name"), str):
            monitor_name = summary["name"].strip() or fallback_name
        selected_count = 0
        for company in companies:
            if not isinstance(company, Mapping):
                raise AlertNotificationError("each report company must be an object")
            monitor_results = company.get("monitor_results")
            if not isinstance(monitor_results, Mapping):
                continue
            result = monitor_results.get(monitor_code)
            if not isinstance(result, Mapping) or result.get("status") != "ALERT":
                continue
            company_code = _required_text(company.get("company_code"), "company_code")
            if (
                normalized_company_codes is not None
                and company_code not in normalized_company_codes
            ):
                continue
            selections.append(
                AlertSelection(
                    monitor_code=monitor_code,
                    monitor_name=monitor_name,
                    company_code=company_code,
                    company_name=_required_text(company.get("company_name"), "company_name"),
                    result=dict(result),
                )
            )
            selected_count += 1
            if selected_count == max_companies_per_monitor:
                break
    return tuple(selections)


def build_company_link_directory(
    record_ids: Sequence[object],
    rows: Sequence[Sequence[object]],
) -> CompanyLinkDirectory:
    """Parse projected Base rows and retain only the main-table relationship key."""

    if len(record_ids) != len(rows):
        raise AlertNotificationError("company record IDs and rows must have the same length")
    companies: dict[str, CompanyLink] = {}
    issues: dict[str, str] = {}
    excluded = 0
    for row_number, (raw_record_id, row) in enumerate(zip(record_ids, rows, strict=True), start=1):
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)) or len(row) != 2:
            raise AlertNotificationError(
                f"company row {row_number} must contain company code and company name"
            )
        company_code = _optional_text(row[0])
        if company_code is None:
            excluded += 1
            continue
        company_name = _optional_text(row[1]) or company_code
        record_id = _optional_text(raw_record_id)
        if company_code in companies or company_code in issues:
            companies.pop(company_code, None)
            issues[company_code] = "公司代码在飞书多维表中存在重复记录"
            continue
        if record_id is None or not record_id.startswith("rec"):
            issues[company_code] = "飞书多维表记录缺少有效 record_id"
            continue
        companies[company_code] = CompanyLink(
            record_id=record_id,
            company_code=company_code,
            company_name=company_name,
        )
    return CompanyLinkDirectory(
        companies=companies,
        issues=issues,
        source_record_count=len(rows),
        excluded_blank_company_count=excluded,
    )


def build_queue_plan(
    report: Mapping[str, object],
    directory: CompanyLinkDirectory,
    *,
    max_companies_per_monitor: int = MAX_ALERT_COMPANIES_PER_MONITOR,
    dashboard_url: str,
    test_push: bool = False,
    company_codes: Collection[str] | None = None,
    monitor_codes: Collection[str] | None = None,
) -> AlertQueuePlan:
    """Build one manually pushable queue row for each company-monitor alert."""

    normalized_dashboard_url = _required_text(dashboard_url, "dashboard_url")
    selections = select_alerts(
        report,
        max_companies_per_monitor=max_companies_per_monitor,
        company_codes=company_codes,
        monitor_codes=monitor_codes,
    )
    period = _period_label(report)
    items: list[AlertQueueItem] = []
    skipped: list[SkippedQueueItem] = []
    for selection in selections:
        company_link = directory.companies.get(selection.company_code)
        if company_link is None:
            skipped.append(
                SkippedQueueItem(
                    company_code=selection.company_code,
                    company_name=selection.company_name,
                    monitor_code=selection.monitor_code,
                    reason_code=(
                        "INVALID_COMPANY_LINK"
                        if selection.company_code in directory.issues
                        else "COMPANY_LINK_NOT_FOUND"
                    ),
                    reason=directory.issues.get(
                        selection.company_code,
                        "未在飞书法人主体主表中找到公司代码",
                    ),
                )
            )
            continue
        items.append(
            AlertQueueItem(
                idempotency_key=_queue_idempotency_key(report, selection),
                company_record_id=company_link.record_id,
                company_code=selection.company_code,
                company_name=selection.company_name,
                monitor_code=selection.monitor_code,
                monitor_name=selection.monitor_name,
                period=period,
                outcome=_result_text(selection.result, "outcome", "待核查"),
                key_values=render_key_values(
                    selection.monitor_code, selection.result.get("values")
                ),
                alert_details=render_alert_details(selection),
                dashboard_url=normalized_dashboard_url,
                test_push=test_push,
            )
        )
    return AlertQueuePlan(
        selections=selections,
        items=tuple(items),
        skipped=tuple(skipped),
    )


def render_key_values(monitor_code: str, raw_values: object) -> str:
    if not isinstance(raw_values, Mapping):
        return ""
    parts: list[str] = []
    for key, label, value_type in _VALUE_FIELDS.get(monitor_code, ()):
        value = raw_values.get(key)
        if value is None or value == "":
            continue
        parts.append(f"{label} {_format_value(value, value_type)}")
    return "；".join(parts)


def render_alert_details(
    selection: AlertSelection,
    *,
    max_candidate_lines: int | None = MAX_CANDIDATE_LINES,
) -> str:
    lines: list[str] = []
    reason = _result_text(selection.result, "reason", "")
    if reason:
        lines.append(f"说明：{reason}")
    lines.extend(
        _render_candidates(
            selection.monitor_code,
            selection.result.get("candidates"),
            max_candidate_lines=max_candidate_lines,
        )
    )
    return "\n".join(lines) or "无额外明细，请查看驾驶舱。"


def _render_candidates(
    monitor_code: str,
    raw_candidates: object,
    *,
    max_candidate_lines: int | None,
) -> list[str]:
    if not isinstance(raw_candidates, list) or not raw_candidates:
        return []
    candidates = [item for item in raw_candidates if isinstance(item, Mapping)]
    if not candidates:
        return []
    lines = [f"示警明细（{len(candidates)}条）："]
    rendered_candidates = (
        candidates if max_candidate_lines is None else candidates[:max_candidate_lines]
    )
    for index, candidate in enumerate(rendered_candidates, start=1):
        if monitor_code == "refund":
            account = _join_account(candidate.get("account_code"), candidate.get("account_name"))
            lines.append(
                f"{index}. {_optional_text(candidate.get('family')) or '未分类'}；"
                f"科目 {account or '未知'}；"
                f"凭证号 {_optional_text(candidate.get('voucher_no')) or '未知'}；"
                f"金额 {_format_value(candidate.get('amount'), 'money')}"
            )
            continue
        if monitor_code == "tax_adjustment_account_accuracy":
            subject = _optional_text(candidate.get("subject")) or "未分类"
            account = _join_account(candidate.get("gl_account"), candidate.get("account_name"))
            line = (
                f"{index}. {subject}；凭证号 "
                f"{_optional_text(candidate.get('voucher_no')) or '未知'}；"
                f"当前科目 {account or '未知'}；"
                f"金额 {_format_value(candidate.get('amount'), 'money')}；"
                f"抬头摘要 {_optional_text(candidate.get('header_text')) or '无'}；"
                f"明细摘要 {_optional_text(candidate.get('detail_text')) or '无'}；"
                "建议入账至 "
                f"{_optional_text(candidate.get('recommended_account')) or '待人工判断'}"
            )
            evidence = _supplementary_evidence(candidate)
            if evidence:
                line += f"；关联事由 {evidence}"
            lines.append(line)
            continue
        lines.append(f"{index}. {json.dumps(candidate, ensure_ascii=False, sort_keys=True)}")
    if max_candidate_lines is not None and len(candidates) > max_candidate_lines:
        lines.append(f"其余 {len(candidates) - max_candidate_lines} 条请在监测驾驶舱中查看。")
    return lines


def _supplementary_evidence(candidate: Mapping[str, object]) -> str:
    values: list[str] = []
    for key in ("hesi_detail_descriptions", "hesi_application_descriptions"):
        value = _optional_text(candidate.get(key))
        if value and value not in values:
            values.append(value)
    return "；".join(values)


def _queue_idempotency_key(
    report: Mapping[str, object],
    selection: AlertSelection,
) -> str:
    canonical = {
        "protocol": QUEUE_PROTOCOL_VERSION,
        "period": [report.get(key) for key in ("fiscal_year", "quarter", "through_period")],
        "company_code": selection.company_code,
        "monitor_code": selection.monitor_code,
        "result": selection.result,
    }
    digest = sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()[:40]
    return f"taxrisk-queue-{digest}"


def _period_label(report: Mapping[str, object]) -> str:
    fiscal_year = _required_int(report.get("fiscal_year"), "fiscal_year")
    quarter = _required_int(report.get("quarter"), "quarter")
    through_period = _required_int(report.get("through_period"), "through_period")
    if quarter not in (1, 2, 3, 4):
        raise AlertNotificationError("quarter must be between 1 and 4")
    if through_period not in range(1, 13):
        raise AlertNotificationError("through_period must be between 1 and 12")
    return f"{fiscal_year}年第{quarter}季度（截至{through_period}月）"


def _result_text(result: Mapping[str, object], key: str, default: str) -> str:
    return _optional_text(result.get(key)) or default


def _join_account(code: object, name: object) -> str:
    parts = [part for part in (_optional_text(code), _optional_text(name)) if part]
    return " ".join(parts)


def _format_value(value: object, value_type: str) -> str:
    if value_type == "text":
        return _optional_text(value) or "未知"
    if value_type == "count":
        try:
            return f"{int(Decimal(str(value)))}条"
        except (InvalidOperation, ValueError, TypeError):
            return f"{value}条"
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return str(value)
    if not number.is_finite():
        return str(value)
    if value_type == "rate":
        return f"{number * Decimal(100):,.2f}%"
    return f"{number:,.2f}元"


def _required_text(value: object, field_name: str) -> str:
    normalized = _optional_text(value)
    if normalized is None:
        raise AlertNotificationError(f"{field_name} must be a non-empty string")
    return normalized


def _requested_monitors(
    monitor_codes: Collection[str] | None,
) -> tuple[tuple[str, str], ...]:
    if monitor_codes is None:
        return MONITORS
    normalized = {_required_text(code, "monitor_codes item") for code in monitor_codes}
    known = {code for code, _ in MONITORS}
    unknown = normalized - known
    if unknown:
        raise AlertNotificationError(f"unknown monitor codes: {','.join(sorted(unknown))}")
    if not normalized:
        raise AlertNotificationError("monitor_codes cannot be empty")
    return tuple(monitor for monitor in MONITORS if monitor[0] in normalized)


def _optional_text(value: object) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (int, Decimal)) and not isinstance(value, bool):
        return str(value)
    return None


def _required_int(value: object, field_name: str) -> int:
    if isinstance(value, bool):
        raise AlertNotificationError(f"{field_name} must be an integer")
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as error:
        raise AlertNotificationError(f"{field_name} must be an integer") from error
    if parsed <= 0:
        raise AlertNotificationError(f"{field_name} must be positive")
    return parsed


__all__ = [
    "AlertNotificationError",
    "AlertQueueItem",
    "AlertQueuePlan",
    "AlertSelection",
    "CompanyLink",
    "CompanyLinkDirectory",
    "MAX_ALERT_COMPANIES_PER_MONITOR",
    "MONITORS",
    "SkippedQueueItem",
    "build_company_link_directory",
    "build_queue_plan",
    "render_alert_details",
    "render_key_values",
    "select_alerts",
]
