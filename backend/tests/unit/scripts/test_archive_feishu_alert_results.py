from __future__ import annotations

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
    values = archives._row_values(_row(), archived_at="2026-07-24 18:30:00")
    payload = dict(zip(archives.ARCHIVE_FIELD_NAMES, values, strict=True))

    assert payload["法人主体"] == [{"id": "rec_company"}]
    assert payload["示警具体明细"] == "检查结论：少计提\n关键数值：差异 100.00元"
    assert payload["当前示警"] is True
    assert payload["来源模式"] == "REAL"


def test_archive_table_is_idempotent_and_retires_only_covered_monitor_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    plan = AlertArchiveTablePlan(
        table_name="季度示警明细-2026Q2",
        cadence="季度",
        period="2026年第2季度",
        monitor_codes=("current_tax_accrual",),
        rows=(_row(key="existing-target"), _row(key="new-target")),
    )
    existing = {
        "existing-target": archives.ExistingArchiveRow(
            record_id="rec_restore",
            archive_key="existing-target",
            monitor_code="current_tax_accrual",
            is_current=False,
        ),
        "old-target": archives.ExistingArchiveRow(
            record_id="rec_retire",
            archive_key="old-target",
            monitor_code="current_tax_accrual",
            is_current=True,
        ),
        "other-monitor": archives.ExistingArchiveRow(
            record_id="rec_keep",
            archive_key="other-monitor",
            monitor_code="deferred_tax",
            is_current=True,
        ),
    }
    created: list[str] = []
    current_updates: list[tuple[tuple[str, ...], bool]] = []

    monkeypatch.setattr(archives, "_ensure_table", lambda *args, **kwargs: ("tbl_archive", False))
    monkeypatch.setattr(archives, "_load_field_ids", lambda *args, **kwargs: _field_ids())
    monkeypatch.setattr(archives, "_ensure_current_view", lambda *args, **kwargs: "vew_current")
    monkeypatch.setattr(archives, "_load_existing_rows", lambda *args, **kwargs: existing)

    def create(
        cli: str,
        *,
        rows: Sequence[AlertArchiveRow],
        **kwargs: object,
    ) -> tuple[str, ...]:
        created.extend(row.archive_key for row in rows)
        return tuple(f"rec_{index}" for index, _ in enumerate(rows))

    def update(
        cli: str,
        *,
        record_ids: Sequence[str],
        value: bool,
        **kwargs: object,
    ) -> None:
        current_updates.append((tuple(record_ids), value))

    monkeypatch.setattr(archives, "_batch_create", create)
    monkeypatch.setattr(archives, "_batch_set_current", update)

    result = archives._archive_table_plan(
        "lark-cli",
        plan,
        base_identity="user",
        base_profile=None,
        archived_at="2026-07-24 18:30:00",
    )

    assert created == ["new-target"]
    assert current_updates == [(('rec_restore',), True), (('rec_retire',), False)]
    assert "rec_keep" not in {record_id for ids, _ in current_updates for record_id in ids}
    assert (result.created_rows, result.restored_rows, result.retired_rows) == (1, 1, 1)


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
