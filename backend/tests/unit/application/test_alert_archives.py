from __future__ import annotations

from copy import deepcopy

import pytest

from tax_risk.application.alert_archives import AlertArchiveError, build_archive_plan
from tax_risk.application.alert_notifications import MONITORS, build_company_link_directory


def _result(*, status: str, outcome: str, value: str = "100") -> dict[str, object]:
    return {
        "status": status,
        "outcome": outcome,
        "reason": None,
        "values": {
            "difference": value,
            "adjustment": value,
            "deviation": "0.05",
            "potential_tax_cost": value,
            "refund_amount": value,
            "business_entertainment_alert_count": "1",
        },
        "candidates": [],
    }


def _report() -> dict[str, object]:
    companies = [
        {
            "company_code": "C1",
            "company_name": "公司1",
            "monitor_results": {
                code: _result(status="ALERT", outcome=f"{name}示警")
                for code, name in MONITORS
            },
        },
        {
            "company_code": "C2",
            "company_name": "公司2",
            "monitor_results": {
                code: _result(status="CLEAR", outcome=f"{name}正常")
                for code, name in MONITORS
            },
        },
    ]
    return {
        "generated_at": "2026-07-24T02:27:47+00:00",
        "fiscal_year": 2026,
        "quarter": 2,
        "through_period": 6,
        "source_mode": "REAL",
        "company_scope": {
            "base_record_count": 3,
            "excluded_blank_company_count": 1,
            "included_company_count": 2,
        },
        "monitor_summary": {code: {"name": name} for code, name in MONITORS},
        "companies": companies,
    }


def _directory() -> object:
    return build_company_link_directory(
        ["rec_c1", "rec_c2"],
        [["C1", "公司1"], ["C2", "公司2"]],
    )


def test_archive_plan_groups_all_alerts_into_period_monthly_and_quarterly_tables() -> None:
    plan = build_archive_plan(
        _report(),
        _directory(),  # type: ignore[arg-type]
        dashboard_url="https://example.test/dashboard",
    )

    assert [table.table_name for table in plan.tables] == [
        "季度示警明细-2026年2季",
        "月度示警明细-2026年06月",
    ]
    quarterly, monthly = plan.tables
    assert len(quarterly.rows) == 4
    assert len(monthly.rows) == 2
    assert {row.company_code for table in plan.tables for row in table.rows} == {"C1"}
    assert all("检查结论：" in row.alert_details for table in plan.tables for row in table.rows)
    assert all("关键数值：" in row.alert_details for table in plan.tables for row in table.rows)
    assert all(row.company_record_id == "rec_c1" for table in plan.tables for row in table.rows)


def test_archive_plan_keeps_every_concrete_candidate_without_queue_truncation() -> None:
    report = _report()
    companies = report["companies"]
    assert isinstance(companies, list)
    first = companies[0]
    assert isinstance(first, dict)
    results = first["monitor_results"]
    assert isinstance(results, dict)
    candidates = [
        {
            "subject": "业务招待费",
            "voucher_no": f"V{index:03d}",
            "gl_account": "6600400000",
            "account_name": "费用-业务招待费",
            "amount": str(index),
            "header_text": f"抬头{index}",
            "detail_text": f"明细{index}",
            "recommended_account": "福利费",
        }
        for index in range(1, 26)
    ]
    adjustment = _result(status="ALERT", outcome="发现疑似错入科目")
    adjustment["candidates"] = candidates
    results["tax_adjustment_account_accuracy"] = adjustment

    plan = build_archive_plan(
        report,
        _directory(),  # type: ignore[arg-type]
        dashboard_url="https://example.test/dashboard",
        monitor_codes={"tax_adjustment_account_accuracy"},
    )

    details = plan.tables[0].rows[0].alert_details
    assert "示警明细（25条）" in details
    assert "25. 业务招待费；凭证号 V025" in details
    assert "其余" not in details


def test_archive_plan_rejects_partial_company_scope() -> None:
    report = _report()
    scope = report["company_scope"]
    assert isinstance(scope, dict)
    scope["included_company_count"] = 1

    with pytest.raises(AlertArchiveError, match="not a complete"):
        build_archive_plan(
            report,
            _directory(),  # type: ignore[arg-type]
            dashboard_url="https://example.test/dashboard",
        )


def test_archive_plan_rejects_non_real_source_report() -> None:
    report = _report()
    report["source_mode"] = "MOCK"

    with pytest.raises(AlertArchiveError, match="only REAL"):
        build_archive_plan(
            report,
            _directory(),  # type: ignore[arg-type]
            dashboard_url="https://example.test/dashboard",
        )


def test_archive_keys_are_idempotent_for_one_report_and_change_for_a_new_batch() -> None:
    report = _report()
    first = build_archive_plan(
        report,
        _directory(),  # type: ignore[arg-type]
        dashboard_url="https://example.test/dashboard",
    )
    second = build_archive_plan(
        deepcopy(report),
        _directory(),  # type: ignore[arg-type]
        dashboard_url="https://example.test/dashboard",
    )
    changed = deepcopy(report)
    changed["generated_at"] = "2026-07-24T03:00:00+00:00"
    third = build_archive_plan(
        changed,
        _directory(),  # type: ignore[arg-type]
        dashboard_url="https://example.test/dashboard",
    )

    assert [row.archive_key for table in first.tables for row in table.rows] == [
        row.archive_key for table in second.tables for row in table.rows
    ]
    assert first.batch_id != third.batch_id
    assert {row.archive_key for table in first.tables for row in table.rows}.isdisjoint(
        {row.archive_key for table in third.tables for row in table.rows}
    )


def test_archive_plan_fails_if_an_alert_company_cannot_link_to_main_base() -> None:
    directory = build_company_link_directory(["rec_c2"], [["C2", "公司2"]])

    with pytest.raises(AlertArchiveError, match="company C1 cannot be linked"):
        build_archive_plan(
            _report(),
            directory,
            dashboard_url="https://example.test/dashboard",
        )
