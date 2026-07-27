from __future__ import annotations

import json
from collections.abc import Sequence

import pytest

from scripts import archive_feishu_alert_results as archives
from scripts.enqueue_feishu_alert_notifications import LarkCliError
from tax_risk.application.alert_archives import AlertArchiveRow, AlertArchiveTablePlan


def _row(*, key: str = "new-key") -> AlertArchiveRow:
    return AlertArchiveRow(
        archive_key=key,
        batch_id="2026Q2-batch",
        company_record_id="rec_company",
        company_code="3000",
        company_name="测试公司",
        cadence="季度",
        period="2026年第2季度",
        monitor_code="current_tax_accrual",
        monitor_name="季度应计提所得税准确性检查",
        outcome="少计提",
        key_values="本季度应计提所得税 100.00元",
        alert_details="检查结论：少计提\n关键数值：差异 100.00元",
        evidence_limited=True,
        report_generated_at="2026-07-24T10:00:00+08:00",
        dashboard_url="https://example.test/dashboard",
        source_mode="REAL",
    )


def _field_ids() -> dict[str, str]:
    return {name: f"fld{index:03d}" for index, name in enumerate(archives.ARCHIVE_FIELD_NAMES)}


def test_archive_row_writes_company_link_and_complete_details() -> None:
    values = archives._row_values(_row())
    payload = dict(zip(archives.WRITABLE_ARCHIVE_FIELD_NAMES, values, strict=True))

    assert payload["法人主体"] == [{"id": "rec_company"}]
    assert payload["示警明细"] == "检查结论：少计提\n关键数值：差异 100.00元"
    assert payload["推送"] is False
    assert payload["推送状态"] == "待推送"
    assert payload["修改的凭证号"] is None
    assert payload["修改的会计年度"] is None
    assert "业财姓名" not in payload


def test_archive_table_always_creates_new_table_rows_and_feedback_workflow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = AlertArchiveTablePlan(
        table_name="季度示警明细-2026年2季",
        cadence="季度",
        period="2026年第2季度",
        monitor_codes=("current_tax_accrual",),
        rows=(_row(key="first"), _row(key="second")),
    )
    created: list[str] = []
    workflows: list[tuple[str, str]] = []

    monkeypatch.setattr(
        archives,
        "_create_execution_table",
        lambda *args, **kwargs: (
            "tbl_archive",
            "季度示警明细-2026年2季-20260724-183000",
        ),
    )
    monkeypatch.setattr(archives, "_load_field_ids", lambda *args, **kwargs: _field_ids())

    def create(
        cli: str,
        *,
        rows: Sequence[AlertArchiveRow],
        **kwargs: object,
    ) -> tuple[str, ...]:
        created.extend(row.archive_key for row in rows)
        return tuple(f"rec_{index}" for index, _ in enumerate(rows))

    def workflow(
        cli: str,
        *,
        table_id: str,
        table_name: str,
        **kwargs: object,
    ) -> str:
        workflows.append((table_id, table_name))
        return "wkf_detail"

    monkeypatch.setattr(archives, "_batch_create", create)
    monkeypatch.setattr(archives, "_create_feedback_workflow", workflow)

    result = archives._archive_table_plan(
        "lark-cli",
        plan,
        base_identity="user",
        base_profile=None,
        executed_at="2026-07-24 18:30:00",
        create_workflow=True,
    )

    assert created == ["first", "second"]
    assert workflows == [("tbl_archive", "季度示警明细-2026年2季-20260724-183000")]
    assert result.created_rows == 2
    assert result.workflow_id == "wkf_detail"


def test_execution_table_name_adds_timestamp_and_sequence_on_collision() -> None:
    tables = [
        {"name": "月度示警明细-2026年06月"},
        {"name": "月度示警明细-2026年06月-20260724-183000"},
    ]

    assert (
        archives._execution_table_name(
            tables,
            "月度示警明细-2026年06月",
            executed_at="2026-07-24 18:30:00",
        )
        == "月度示警明细-2026年06月-20260724-183000-2"
    )


def test_create_execution_table_never_reuses_the_existing_period_table(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(
        archives,
        "_list_tables",
        lambda *args, **kwargs: [{"id": "tbl_old", "name": "月度示警明细-2026年06月"}],
    )

    def run(cli: str, arguments: list[str], **kwargs: object) -> dict[str, object]:
        calls.append(arguments)
        return {"data": {"table": {"id": "tbl_new"}}}

    monkeypatch.setattr(archives, "_run_lark_cli", run)

    table_id, table_name = archives._create_execution_table(
        "lark-cli",
        requested_name="月度示警明细-2026年06月",
        executed_at="2026-07-24 18:30:00",
        base_identity="user",
        base_profile=None,
    )

    assert table_id == "tbl_new"
    assert table_name == "月度示警明细-2026年06月-20260724-183000"
    assert calls[0][calls[0].index("--name") + 1] == table_name
    fields = json.loads(calls[0][calls[0].index("--fields") + 1])
    assert len(fields) == 19
    assert {field["name"] for field in fields} == set(archives.ARCHIVE_FIELD_NAMES)


def test_feedback_workflow_targets_the_new_detail_record() -> None:
    field_ids = _field_ids()
    workflow = archives._feedback_workflow_definition(
        "月度示警明细-2026年06月",
        field_ids,
    )
    steps = workflow["steps"]
    assert isinstance(steps, list)
    trigger, message, update = steps

    assert trigger["data"]["table_name"] == "月度示警明细-2026年06月"  # type: ignore[index]
    assert message["data"]["receiver"] == [  # type: ignore[index]
        {
            "value_type": "ref",
            "value": (
                f"$.step_manual_push.{field_ids['法人主体']}.{archives.MAIN_FINANCE_FIELD_ID}"
            ),
        }
    ]
    buttons = message["data"]["btn_list"]  # type: ignore[index]
    assert buttons[1]["link"] == [{"value_type": "ref", "value": "$.step_manual_push.recordLink"}]
    assert update["data"]["table_name"] == "月度示警明细-2026年06月"  # type: ignore[index]


def test_load_field_ids_rejects_wrong_company_link_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fields: list[dict[str, object]] = []
    for index, specification in enumerate(archives.ARCHIVE_FIELDS):
        field = dict(specification)
        field["id"] = f"fld{index:03d}"
        if field["name"] == "法人主体":
            field["link_table"] = "tbl_wrong"
        fields.append(field)

    monkeypatch.setattr(
        archives,
        "_run_lark_cli",
        lambda *args, **kwargs: {"data": {"fields": fields}},
    )

    with pytest.raises(LarkCliError, match="must link"):
        archives._load_field_ids(
            "lark-cli",
            table_id="tbl_archive",
            base_identity="user",
            base_profile=None,
        )
