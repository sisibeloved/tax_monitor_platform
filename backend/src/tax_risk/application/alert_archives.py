"""Plan complete period-scoped Lark Base archives for full-company alerts."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from hashlib import sha256
import json
from typing import Final, Literal

from tax_risk.application.alert_notifications import (
    AlertSelection,
    CompanyLinkDirectory,
    MONITORS,
    render_alert_details,
    render_key_values,
)


ArchiveCadence = Literal["月度", "季度"]

MONITOR_CADENCE: Final[dict[str, ArchiveCadence]] = {
    "current_tax_accrual": "季度",
    "deferred_tax": "季度",
    "refund": "月度",
    "tax_burden": "季度",
    "potential_tax_cost": "季度",
    "tax_adjustment_account_accuracy": "月度",
}
_MONITOR_NAMES: Final[dict[str, str]] = dict(MONITORS)
_PLACEHOLDER_DETAILS: Final[str] = "无额外明细，请查看驾驶舱。"


class AlertArchiveError(ValueError):
    """Raised when a full report cannot be archived without losing alert evidence."""


@dataclass(frozen=True, slots=True)
class AlertArchiveRow:
    archive_key: str
    batch_id: str
    company_record_id: str
    company_code: str
    company_name: str
    cadence: ArchiveCadence
    period: str
    monitor_code: str
    monitor_name: str
    outcome: str
    key_values: str
    alert_details: str
    evidence_limited: bool
    report_generated_at: str
    dashboard_url: str
    source_mode: str


@dataclass(frozen=True, slots=True)
class AlertArchiveTablePlan:
    table_name: str
    cadence: ArchiveCadence
    period: str
    monitor_codes: tuple[str, ...]
    rows: tuple[AlertArchiveRow, ...]


@dataclass(frozen=True, slots=True)
class AlertArchivePlan:
    batch_id: str
    report_generated_at: str
    tables: tuple[AlertArchiveTablePlan, ...]


def build_archive_plan(
    report: Mapping[str, object],
    directory: CompanyLinkDirectory,
    *,
    dashboard_url: str,
    monitor_codes: Collection[str] | None = None,
    require_full_scope: bool = True,
) -> AlertArchivePlan:
    """Build monthly and quarterly table plans with every ALERT company included."""

    companies = _companies(report)
    if require_full_scope:
        _assert_full_scope(report, company_count=len(companies))
    requested_codes = _requested_monitor_codes(monitor_codes)
    fiscal_year = _positive_int(report.get("fiscal_year"), "fiscal_year")
    quarter = _positive_int(report.get("quarter"), "quarter")
    through_period = _positive_int(report.get("through_period"), "through_period")
    if quarter not in (1, 2, 3, 4):
        raise AlertArchiveError("quarter must be between 1 and 4")
    if through_period not in range(1, 13):
        raise AlertArchiveError("through_period must be between 1 and 12")
    report_generated_at = _required_text(report.get("generated_at"), "generated_at")
    source_mode = _required_text(report.get("source_mode"), "source_mode")
    if source_mode != "REAL":
        raise AlertArchiveError("only REAL source reports can be archived")
    normalized_dashboard_url = _required_text(dashboard_url, "dashboard_url")
    batch_id = _batch_id(report_generated_at, fiscal_year, quarter, through_period)

    rows_by_cadence: dict[ArchiveCadence, list[AlertArchiveRow]] = {
        "月度": [],
        "季度": [],
    }
    for monitor_code in requested_codes:
        monitor_name = _monitor_name(report, monitor_code)
        cadence = MONITOR_CADENCE[monitor_code]
        period = _period_label(cadence, fiscal_year, quarter, through_period)
        for company in companies:
            selection = _alert_selection(company, monitor_code, monitor_name)
            if selection is None:
                continue
            company_link = directory.companies.get(selection.company_code)
            if company_link is None:
                reason = directory.issues.get(
                    selection.company_code,
                    "未在飞书法人主体主表中找到公司代码",
                )
                raise AlertArchiveError(
                    f"company {selection.company_code} cannot be linked for archive: {reason}"
                )
            key_values = render_key_values(monitor_code, selection.result.get("values"))
            details = _specific_alert_details(selection, key_values=key_values)
            rows_by_cadence[cadence].append(
                AlertArchiveRow(
                    archive_key=_archive_key(batch_id, selection),
                    batch_id=batch_id,
                    company_record_id=company_link.record_id,
                    company_code=selection.company_code,
                    company_name=selection.company_name,
                    cadence=cadence,
                    period=period,
                    monitor_code=monitor_code,
                    monitor_name=monitor_name,
                    outcome=_required_text(selection.result.get("outcome"), "outcome"),
                    key_values=key_values,
                    alert_details=details,
                    evidence_limited=selection.result.get("evidence_limited") is True,
                    report_generated_at=report_generated_at,
                    dashboard_url=normalized_dashboard_url,
                    source_mode=source_mode,
                )
            )

    table_plans: list[AlertArchiveTablePlan] = []
    for cadence in ("季度", "月度"):
        cadence_codes = tuple(
            code for code in requested_codes if MONITOR_CADENCE[code] == cadence
        )
        if not cadence_codes:
            continue
        period = _period_label(cadence, fiscal_year, quarter, through_period)
        table_plans.append(
            AlertArchiveTablePlan(
                table_name=_table_name(cadence, fiscal_year, quarter, through_period),
                cadence=cadence,
                period=period,
                monitor_codes=cadence_codes,
                rows=tuple(rows_by_cadence[cadence]),
            )
        )
    return AlertArchivePlan(
        batch_id=batch_id,
        report_generated_at=report_generated_at,
        tables=tuple(table_plans),
    )


def _companies(report: Mapping[str, object]) -> list[Mapping[str, object]]:
    raw_companies = report.get("companies")
    if not isinstance(raw_companies, list):
        raise AlertArchiveError("report.companies must be a list")
    companies: list[Mapping[str, object]] = []
    seen_codes: set[str] = set()
    for raw_company in raw_companies:
        if not isinstance(raw_company, Mapping):
            raise AlertArchiveError("each report company must be an object")
        code = _required_text(raw_company.get("company_code"), "company_code")
        if code in seen_codes:
            raise AlertArchiveError(f"report contains duplicate company code {code}")
        seen_codes.add(code)
        companies.append(raw_company)
    return companies


def _assert_full_scope(report: Mapping[str, object], *, company_count: int) -> None:
    scope = report.get("company_scope")
    if not isinstance(scope, Mapping):
        raise AlertArchiveError("full-company archive requires report.company_scope")
    base_count = _positive_int(scope.get("base_record_count"), "base_record_count")
    excluded = _nonnegative_int(
        scope.get("excluded_blank_company_count"),
        "excluded_blank_company_count",
    )
    included = _positive_int(scope.get("included_company_count"), "included_company_count")
    if included != company_count or included != base_count - excluded:
        raise AlertArchiveError("report is not a complete nonblank-company Base scope")


def _requested_monitor_codes(monitor_codes: Collection[str] | None) -> tuple[str, ...]:
    if monitor_codes is None:
        return tuple(code for code, _ in MONITORS)
    normalized = {_required_text(code, "monitor_codes item") for code in monitor_codes}
    unknown = normalized - set(MONITOR_CADENCE)
    if unknown:
        raise AlertArchiveError(f"unknown monitor codes: {','.join(sorted(unknown))}")
    if not normalized:
        raise AlertArchiveError("monitor_codes cannot be empty")
    return tuple(code for code, _ in MONITORS if code in normalized)


def _monitor_name(report: Mapping[str, object], monitor_code: str) -> str:
    summary = report.get("monitor_summary")
    if isinstance(summary, Mapping):
        raw_monitor = summary.get(monitor_code)
        if isinstance(raw_monitor, Mapping):
            name = raw_monitor.get("name")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return _MONITOR_NAMES[monitor_code]


def _alert_selection(
    company: Mapping[str, object],
    monitor_code: str,
    monitor_name: str,
) -> AlertSelection | None:
    results = company.get("monitor_results")
    if not isinstance(results, Mapping):
        raise AlertArchiveError("company.monitor_results must be an object")
    result = results.get(monitor_code)
    if not isinstance(result, Mapping) or result.get("status") != "ALERT":
        return None
    return AlertSelection(
        monitor_code=monitor_code,
        monitor_name=monitor_name,
        company_code=_required_text(company.get("company_code"), "company_code"),
        company_name=_required_text(company.get("company_name"), "company_name"),
        result=dict(result),
    )


def _specific_alert_details(selection: AlertSelection, *, key_values: str) -> str:
    parts = [f"检查结论：{_required_text(selection.result.get('outcome'), 'outcome')}"]
    rendered = render_alert_details(selection, max_candidate_lines=None)
    if rendered != _PLACEHOLDER_DETAILS:
        parts.append(rendered)
    if key_values:
        parts.append(f"关键数值：{key_values}")
    if len(parts) == 1:
        raise AlertArchiveError(
            f"alert {selection.company_code}/{selection.monitor_code} lacks concrete details"
        )
    return "\n".join(parts)


def _batch_id(generated_at: str, year: int, quarter: int, period: int) -> str:
    digest = sha256(
        f"{generated_at}|{year}|{quarter}|{period}".encode("utf-8")
    ).hexdigest()[:16]
    return f"{year}Q{quarter}-{digest}"


def _archive_key(batch_id: str, selection: AlertSelection) -> str:
    canonical = {
        "batch_id": batch_id,
        "company_code": selection.company_code,
        "monitor_code": selection.monitor_code,
    }
    digest = sha256(
        json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:40]
    return f"taxrisk-archive-{digest}"


def _table_name(cadence: ArchiveCadence, year: int, quarter: int, period: int) -> str:
    if cadence == "季度":
        return f"季度示警明细-{year}年{quarter}季"
    return f"月度示警明细-{year}年{period:02d}月"


def _period_label(cadence: ArchiveCadence, year: int, quarter: int, period: int) -> str:
    if cadence == "季度":
        return f"{year}年第{quarter}季度"
    return f"{year}年{period:02d}月"


def _required_text(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AlertArchiveError(f"{field_name} must be a non-empty string")
    return value.strip()


def _positive_int(value: object, field_name: str) -> int:
    parsed = _integer(value, field_name)
    if parsed <= 0:
        raise AlertArchiveError(f"{field_name} must be positive")
    return parsed


def _nonnegative_int(value: object, field_name: str) -> int:
    parsed = _integer(value, field_name)
    if parsed < 0:
        raise AlertArchiveError(f"{field_name} must be nonnegative")
    return parsed


def _integer(value: object, field_name: str) -> int:
    if isinstance(value, bool):
        raise AlertArchiveError(f"{field_name} must be an integer")
    try:
        return int(str(value))
    except (TypeError, ValueError) as error:
        raise AlertArchiveError(f"{field_name} must be an integer") from error


__all__ = [
    "AlertArchiveError",
    "AlertArchivePlan",
    "AlertArchiveRow",
    "AlertArchiveTablePlan",
    "ArchiveCadence",
    "MONITOR_CADENCE",
    "build_archive_plan",
]
