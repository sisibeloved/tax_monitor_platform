import json
from pathlib import Path

import pytest

from tax_risk.application.tax_adjustment_accounts.web_report import (
    MONITOR_CODE,
    TaxAdjustmentWebReportError,
    merge_tax_adjustment_report,
)


def _base_report() -> dict[str, object]:
    return {
        "fiscal_year": 2026,
        "through_period": 6,
        "runtime": {},
        "monitor_summary": {},
        "companies": [
            {
                "company_code": "3CC0",
                "company_name": "杭州海亮研学旅行有限公司",
                "monitor_results": {},
            },
            {
                "company_code": "3000",
                "company_name": "浙江海亮教育发展有限公司",
                "monitor_results": {},
            },
        ],
    }


def _result_payload() -> dict[str, object]:
    common = {
        "fiscal_year": "2026",
        "through_month": 6,
        "welfare_status": "CLEAR",
        "donation_status": "CLEAR",
        "business_entertainment_status": "CLEAR",
        "welfare_error": "",
        "donation_error": "",
        "business_entertainment_error": "",
        "welfare_alert_count": 0,
        "donation_alert_count": 0,
        "donation_abnormal_candidate_count": 0,
        "donation_cumulative": "0",
        "business_entertainment_cumulative": "0",
        "business_entertainment_detail_count": 0,
        "business_entertainment_alert_count": 0,
        "business_entertainment_alert_amount": "0",
        "business_entertainment_hesi_detail_count": 0,
        "business_entertainment_hesi_invoice_count": 0,
        "business_entertainment_hesi_application_count": 0,
        "business_entertainment_evidence_status": "NOT_REQUIRED",
    }
    return {
        "scope": {"fiscal_year": "2026", "through_month": 6},
        "rows": [
            {
                **common,
                "company_code": "3CC0",
                "welfare_cumulative": "30510.23",
                "salary_cumulative": "4375576.45",
                "welfare_deduction_limit": "612580.7030",
                "welfare_adjustment": "0",
                "welfare_detail_selected": "false",
                "welfare_abnormal_candidate_count": 1,
                "welfare_alert_amount": "0",
            },
            {
                **common,
                "company_code": "3000",
                "welfare_cumulative": "309160.37",
                "salary_cumulative": None,
                "welfare_deduction_limit": None,
                "welfare_adjustment": None,
                "welfare_detail_selected": "false",
                "welfare_abnormal_candidate_count": 0,
                "welfare_alert_amount": "0",
            },
        ],
    }


def _candidate_payload() -> dict[str, object]:
    return {
        "scope": {"fiscal_year": "2026", "through_month": 6},
        "companies": [
            {
                "company_code": "3CC0",
                "rows": {
                    "WELFARE": [
                        {
                            "company": "3CC0",
                            "companyname": "杭州海亮研学旅行有限公司",
                            "fiscal_year": "2026",
                            "fiscal_period": "003",
                            "voucher_no": "1000000150",
                            "header_text": "供应商公务接待",
                            "detail_text": "项目组有供应商到访，需进行公务接待",
                            "amount_ksl": "500.00",
                            "gl_account": "6600080000",
                            "account_name": "福利费",
                            "project_code": "",
                            "project_name": "",
                            "debit_credit_flag": "S",
                            "group_currency": "CNY",
                            "original_system_doc_no": "SAP-1",
                        }
                    ],
                    "DONATION": [],
                },
                "business_entertainment_candidates": [],
            },
            {
                "company_code": "3000",
                "rows": {"WELFARE": [], "DONATION": []},
                "business_entertainment_candidates": [],
            },
        ],
    }


def _write(path: Path, payload: dict[str, object]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_merge_publishes_company_results_summary_and_candidate_evidence(
    tmp_path: Path,
) -> None:
    result_path = tmp_path / "result.json"
    candidate_path = tmp_path / "candidates.json"
    _write(result_path, _result_payload())
    _write(candidate_path, _candidate_payload())

    merged = merge_tax_adjustment_report(
        _base_report(),
        result_path=result_path,
        candidate_path=candidate_path,
    )

    summary = merged["monitor_summary"][MONITOR_CODE]
    assert summary == {
        "name": "纳税调增科目准确性检查",
        "total": 2,
        "ALERT": 0,
        "CLEAR": 2,
        "BLOCKED": 0,
        "NOT_APPLICABLE": 0,
    }
    company = merged["companies"][0]
    result = company["monitor_results"][MONITOR_CODE]
    assert result["outcome"] == "存在候选但未达到调增门槛"
    assert result["values"]["welfare_adjustment"] == "0"
    assert result["subject_results"]["business_entertainment"]["status"] == "CLEAR"
    assert result["subject_results"]["business_entertainment"]["candidates"] == []
    assert result["subject_results"]["welfare"]["status"] == "CLEAR"
    assert (
        result["subject_results"]["welfare"]["outcome"]
        == "存在候选但未达到调增门槛"
    )
    assert result["subject_results"]["donation"]["status"] == "CLEAR"
    assert result["candidates"] == [
        {
            "candidate_no": "1",
            "subject": "福利费",
            "fiscal_period": "003",
            "voucher_no": "1000000150",
            "original_system_doc_no": "SAP-1",
            "gl_account": "6600080000",
            "account_name": "福利费",
            "header_text": "供应商公务接待",
            "detail_text": "项目组有供应商到访，需进行公务接待",
            "amount": "500.00",
            "currency": "CNY",
            "classification": "业务招待费异常",
            "matched_keywords": "供应商",
            "recommended_account": "业务招待费",
            "recommendation_basis": "行项目摘要命中关键词：供应商",
        }
    ]
    runtime = merged["runtime"][MONITOR_CODE]
    assert runtime["candidate_company_count"] == 1
    assert runtime["candidate_detail_count"] == 1


def test_merge_reclassifies_customer_success_center_with_current_rules(
    tmp_path: Path,
) -> None:
    result_path = tmp_path / "result.json"
    candidate_path = tmp_path / "candidates.json"
    candidate_payload = _candidate_payload()
    candidate_companies = candidate_payload["companies"]
    assert isinstance(candidate_companies, list)
    candidate_company = candidate_companies[0]
    assert isinstance(candidate_company, dict)
    subject_rows = candidate_company["rows"]
    assert isinstance(subject_rows, dict)
    welfare_rows = subject_rows["WELFARE"]
    assert isinstance(welfare_rows, list)
    welfare_row = welfare_rows[0]
    assert isinstance(welfare_row, dict)
    welfare_row["detail_text"] = "吕佳楠报销客户成功中心何炜淼生日采购"
    _write(result_path, _result_payload())
    _write(candidate_path, candidate_payload)

    merged = merge_tax_adjustment_report(
        _base_report(),
        result_path=result_path,
        candidate_path=candidate_path,
    )

    company = merged["companies"][0]
    result = company["monitor_results"][MONITOR_CODE]
    assert result["outcome"] == "未发现需示警的错入科目"
    assert result["values"]["welfare_abnormal_candidate_count"] == "0"
    assert result["candidates"] == []
    runtime = merged["runtime"][MONITOR_CODE]
    assert runtime["candidate_company_count"] == 0
    assert runtime["candidate_detail_count"] == 0


def test_merge_includes_business_entertainment_evidence_and_alert(tmp_path: Path) -> None:
    result_payload = _result_payload()
    result_rows = result_payload["rows"]
    assert isinstance(result_rows, list)
    first_result = result_rows[0]
    assert isinstance(first_result, dict)
    first_result.update(
        {
            "business_entertainment_status": "ALERT",
            "business_entertainment_cumulative": "1200.00",
            "business_entertainment_detail_count": 1,
            "business_entertainment_alert_count": 1,
            "business_entertainment_alert_amount": "1200.00",
            "business_entertainment_hesi_detail_count": 3,
            "business_entertainment_hesi_invoice_count": 2,
            "business_entertainment_hesi_application_count": 1,
            "business_entertainment_evidence_status": "COMPLETE",
        }
    )
    candidate_payload = _candidate_payload()
    candidate_companies = candidate_payload["companies"]
    assert isinstance(candidate_companies, list)
    first_company = candidate_companies[0]
    assert isinstance(first_company, dict)
    first_company["business_entertainment_candidates"] = [
        {
            "candidate_no": "1",
            "company_code": "3CC0",
            "subject": "业务招待费",
            "fiscal_period": "006",
            "voucher_no": "4900000010",
            "original_system_doc_no": "HSB26000001",
            "gl_account": "6600400000",
            "account_name": "业务招待费",
            "header_text": "员工培训餐",
            "detail_text": "培训餐",
            "amount": "1200.00",
            "currency": "CNY",
            "classification": "可能应归福利费",
            "matched_keywords": "培训餐",
            "recommended_account": "福利费",
            "recommendation_basis": "摘要命中培训餐",
            "hesi_detail_descriptions": "内部培训餐",
            "hesi_application_descriptions": "员工培训会议",
        }
    ]
    result_path = tmp_path / "result.json"
    candidate_path = tmp_path / "candidates.json"
    _write(result_path, result_payload)
    _write(candidate_path, candidate_payload)

    merged = merge_tax_adjustment_report(
        _base_report(),
        result_path=result_path,
        candidate_path=candidate_path,
    )

    result = merged["companies"][0]["monitor_results"][MONITOR_CODE]
    assert result["status"] == "ALERT"
    assert result["values"]["business_entertainment_alert_count"] == "1"
    assert result["candidates"][1]["subject"] == "业务招待费"
    assert result["candidates"][1]["recommended_account"] == "福利费"
    assert result["candidates"][1]["hesi_detail_descriptions"] == "内部培训餐"
    assert result["candidates"][1]["hesi_application_descriptions"] == "员工培训会议"
    business_result = result["subject_results"]["business_entertainment"]
    assert business_result["status"] == "ALERT"
    assert business_result["outcome"] == "发现业务招待费疑似错入"
    assert len(business_result["candidates"]) == 1
    assert business_result["candidates"][0]["hesi_detail_descriptions"] == "内部培训餐"
    welfare_result = result["subject_results"]["welfare"]
    assert welfare_result["status"] == "CLEAR"
    assert len(welfare_result["candidates"]) == 1
    runtime = merged["runtime"][MONITOR_CODE]
    assert runtime["business_entertainment_evaluated_company_count"] == 1
    assert runtime["business_entertainment_alert_company_count"] == 1


def test_merge_marks_monitor_blocked_when_result_file_is_missing(tmp_path: Path) -> None:
    merged = merge_tax_adjustment_report(
        _base_report(),
        result_path=tmp_path / "missing.json",
    )

    assert merged["monitor_summary"][MONITOR_CODE]["BLOCKED"] == 2
    assert (
        merged["companies"][0]["monitor_results"][MONITOR_CODE]["reason"]
        == "纳税调增科目全量结果文件不存在"
    )


def test_merge_rejects_stale_period(tmp_path: Path) -> None:
    payload = _result_payload()
    payload["scope"]["through_month"] = 3
    result_path = tmp_path / "stale.json"
    _write(result_path, payload)

    with pytest.raises(TaxAdjustmentWebReportError, match="period does not match"):
        merge_tax_adjustment_report(_base_report(), result_path=result_path)
