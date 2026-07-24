from __future__ import annotations

from copy import deepcopy

import pytest

from tax_risk.application.alert_notifications import (
    AlertNotificationError,
    CompanyLinkDirectory,
    MONITORS,
    build_company_link_directory,
    build_queue_plan,
    select_alerts,
)


def _result(*, status: str = "ALERT", outcome: str = "示警", **values: str) -> dict[str, object]:
    return {
        "status": status,
        "outcome": outcome,
        "reason": None,
        "values": values,
    }


def _report() -> dict[str, object]:
    monitor_summary = {code: {"name": name} for code, name in MONITORS}
    companies: list[dict[str, object]] = []
    for index in range(1, 6):
        results = {
            code: _result(
                outcome=f"{name}-{index}",
                difference=str(index * 100),
                tax_rate="0.25",
            )
            for code, name in MONITORS
        }
        companies.append(
            {
                "company_code": f"C{index}",
                "company_name": f"公司{index}",
                "monitor_results": results,
            }
        )
    return {
        "generated_at": "2026-07-23T10:55:38+00:00",
        "fiscal_year": 2026,
        "quarter": 2,
        "through_period": 6,
        "monitor_summary": monitor_summary,
        "companies": companies,
    }


def _directory(*company_codes: str) -> CompanyLinkDirectory:
    return build_company_link_directory(
        [f"rec_{code.lower()}" for code in company_codes],
        [[code, f"公司{code.removeprefix('C')}"] for code in company_codes],
    )


def test_selects_at_most_three_alert_companies_per_monitor_in_report_order() -> None:
    selections = select_alerts(_report())

    assert len(selections) == 18
    for monitor_code, _ in MONITORS:
        assert [
            selection.company_code
            for selection in selections
            if selection.monitor_code == monitor_code
        ] == ["C1", "C2", "C3"]


def test_selection_can_target_specific_companies_without_changing_monitor_cap() -> None:
    selections = select_alerts(_report(), company_codes={"C2", "C4"})

    assert len(selections) == 12
    assert {selection.company_code for selection in selections} == {"C2", "C4"}


def test_selection_rejects_limits_over_pilot_safety_cap() -> None:
    with pytest.raises(AlertNotificationError, match="between 1 and 3"):
        select_alerts(_report(), max_companies_per_monitor=4)


def test_queue_plan_creates_one_independent_row_per_company_and_monitor() -> None:
    plan = build_queue_plan(
        _report(),
        _directory("C1", "C2", "C3"),
        dashboard_url="https://example.test/dashboard",
        test_push=True,
    )

    assert len(plan.selections) == 18
    assert len(plan.items) == 18
    assert not plan.skipped
    first = plan.items[0]
    assert first.company_code == "C1"
    assert first.monitor_code == "current_tax_accrual"
    assert first.company_record_id == "rec_c1"
    assert first.period == "2026年第2季度（截至6月）"
    assert first.dashboard_url == "https://example.test/dashboard"
    assert first.test_push is True
    assert first.idempotency_key.startswith("taxrisk-queue-")


def test_missing_or_duplicate_company_link_is_skipped_without_guessing() -> None:
    directory = build_company_link_directory(
        ["rec_c1", "rec_c2a", "rec_c2b"],
        [["C1", "公司1"], ["C2", "公司2"], ["C2", "公司2"]],
    )

    plan = build_queue_plan(
        _report(),
        directory,
        dashboard_url="https://example.test/dashboard",
    )

    assert {item.company_code for item in plan.items} == {"C1"}
    assert {skipped.company_code for skipped in plan.skipped} == {"C2", "C3"}
    duplicate = next(item for item in plan.skipped if item.company_code == "C2")
    assert duplicate.reason_code == "INVALID_COMPANY_LINK"
    assert "重复" in duplicate.reason
    missing = next(item for item in plan.skipped if item.company_code == "C3")
    assert missing.reason_code == "COMPANY_LINK_NOT_FOUND"


def test_company_directory_excludes_blank_codes_and_rejects_invalid_record_ids() -> None:
    directory = build_company_link_directory(
        ["rec_blank", "invalid"],
        [[None, "无代码公司"], ["C1", "公司1"]],
    )

    assert directory.source_record_count == 2
    assert directory.excluded_blank_company_count == 1
    assert not directory.companies
    assert "record_id" in directory.issues["C1"]


def test_queue_details_include_refund_and_tax_adjustment_evidence() -> None:
    report = _report()
    companies = report["companies"]
    assert isinstance(companies, list)
    company = companies[0]
    assert isinstance(company, dict)
    monitor_results = company["monitor_results"]
    assert isinstance(monitor_results, dict)
    monitor_results["refund"] = {
        "status": "ALERT",
        "outcome": "存在多个等额候选",
        "reason": "结果仅作接口验证",
        "values": {"refund_amount": "797109.5125", "match_count": "2"},
        "candidates": [
            {
                "family": "所得税费用",
                "account_code": "6801030000",
                "account_name": "所得税费用-以前年度所得税费用",
                "voucher_no": "1000001452",
                "amount": "797109.51",
            }
        ],
    }
    monitor_results["tax_adjustment_account_accuracy"] = {
        "status": "ALERT",
        "outcome": "发现疑似错入科目",
        "values": {
            "business_entertainment_alert_count": "1",
            "business_entertainment_alert_amount": "541",
        },
        "candidates": [
            {
                "subject": "业务招待费",
                "voucher_no": "1100000056",
                "gl_account": "6600400000",
                "account_name": "费用-业务招待费",
                "header_text": "对公费用",
                "detail_text": "工会年会水果",
                "amount": "541",
                "recommended_account": "福利费",
                "hesi_detail_descriptions": "年会水果报销",
                "hesi_application_descriptions": "年会接待申请",
            }
        ],
    }

    plan = build_queue_plan(
        report,
        _directory("C1", "C2", "C3"),
        dashboard_url="https://example.test/dashboard",
    )
    refund = next(
        item for item in plan.items if item.company_code == "C1" and item.monitor_code == "refund"
    )
    adjustment = next(
        item
        for item in plan.items
        if item.company_code == "C1" and item.monitor_code == "tax_adjustment_account_accuracy"
    )

    assert "所得税退税金额 797,109.51元" in refund.key_values
    assert "6801030000 所得税费用-以前年度所得税费用" in refund.alert_details
    assert "凭证号 1000001452" in refund.alert_details
    assert "当前科目 6600400000 费用-业务招待费" in adjustment.alert_details
    assert "抬头摘要 对公费用；明细摘要 工会年会水果" in adjustment.alert_details
    assert "建议入账至 福利费" in adjustment.alert_details
    assert "关联事由 年会水果报销；年会接待申请" in adjustment.alert_details


def test_queue_idempotency_key_is_stable_and_contains_no_recipient_identity() -> None:
    report = _report()
    directory = _directory("C1", "C2", "C3")

    first = build_queue_plan(report, directory, dashboard_url="https://example.test/dashboard")
    second = build_queue_plan(
        deepcopy(report),
        directory,
        dashboard_url="https://example.test/dashboard",
    )

    assert [item.idempotency_key for item in first.items] == [
        item.idempotency_key for item in second.items
    ]
    assert "open_id" not in repr(first)
    assert "recipient" not in repr(first)
