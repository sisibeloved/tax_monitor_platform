"""Archive full-company alert results into period-scoped Lark Base tables."""

from __future__ import annotations

import argparse
from collections.abc import Collection, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
import json
from pathlib import Path
import shutil
import sys
from typing import Final, Iterator, cast
from uuid import uuid4


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPO_ROOT / "backend"
BACKEND_SRC = REPO_ROOT / "backend" / "src"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from scripts.enqueue_feishu_alert_notifications import (  # noqa: E402
    BASE_TOKEN,
    DEFAULT_BASE_PROFILE,
    DEFAULT_DASHBOARD_URL,
    MAIN_TABLE_ID,
    LarkCliError,
    _load_company_directory,
    _load_json_object,
    _load_projected_records,
    _run_lark_cli,
)
from tax_risk.application.alert_archives import (  # noqa: E402
    AlertArchiveError,
    AlertArchivePlan,
    AlertArchiveRow,
    AlertArchiveTablePlan,
    build_archive_plan,
)


CURRENT_VIEW_NAME: Final[str] = "当前示警"
BATCH_LIMIT: Final[int] = 200

ARCHIVE_FIELDS: Final[tuple[dict[str, object], ...]] = (
    {"type": "text", "name": "归档唯一键"},
    {"type": "text", "name": "检测批次"},
    {
        "type": "link",
        "name": "法人主体",
        "link_table": MAIN_TABLE_ID,
        "bidirectional": False,
    },
    {"type": "text", "name": "公司代码"},
    {"type": "text", "name": "公司名称"},
    {"type": "text", "name": "监测频率"},
    {"type": "text", "name": "监测期间"},
    {"type": "text", "name": "能力代码"},
    {"type": "text", "name": "示警能力"},
    {"type": "text", "name": "检查结论"},
    {"type": "text", "name": "关键数值"},
    {"type": "text", "name": "示警具体明细"},
    {"type": "checkbox", "name": "证据受限"},
    {"type": "checkbox", "name": "当前示警"},
    {"type": "text", "name": "来源模式"},
    {
        "type": "datetime",
        "name": "报告生成时间",
        "style": {"format": "yyyy-MM-dd HH:mm"},
    },
    {
        "type": "datetime",
        "name": "归档时间",
        "style": {"format": "yyyy-MM-dd HH:mm"},
    },
    {
        "type": "text",
        "name": "驾驶舱链接",
        "style": {"type": "url"},
    },
)

ARCHIVE_FIELD_NAMES: Final[tuple[str, ...]] = tuple(
    cast(str, field["name"]) for field in ARCHIVE_FIELDS
)


@dataclass(frozen=True, slots=True)
class ExistingArchiveRow:
    record_id: str
    archive_key: str
    monitor_code: str
    is_current: bool


@dataclass(frozen=True, slots=True)
class ArchiveWriteResult:
    table_id: str
    table_name: str
    created_rows: int
    restored_rows: int
    retired_rows: int


def _list_tables(
    cli: str,
    *,
    base_identity: str,
    base_profile: str | None,
) -> list[dict[str, object]]:
    envelope = _run_lark_cli(
        cli,
        [
            "base",
            "+table-list",
            "--base-token",
            BASE_TOKEN,
            "--as",
            base_identity,
            "--format",
            "json",
        ],
        expected_identity=base_identity,
        profile=base_profile,
    )
    data = envelope.get("data")
    tables = data.get("tables") if isinstance(data, Mapping) else None
    if not isinstance(tables, list) or any(not isinstance(item, dict) for item in tables):
        raise LarkCliError("Lark Base table list has an unexpected shape")
    return cast(list[dict[str, object]], tables)


def _find_table_id(tables: Sequence[Mapping[str, object]], table_name: str) -> str | None:
    matches = [
        str(table.get("id") or "").strip()
        for table in tables
        if table.get("name") == table_name
    ]
    if len(matches) > 1:
        raise LarkCliError(f"Lark Base contains duplicate table name {table_name}")
    if not matches:
        return None
    table_id = matches[0]
    if not table_id.startswith("tbl"):
        raise LarkCliError(f"Lark Base returned an invalid table ID for {table_name}")
    return table_id


def _ensure_table(
    cli: str,
    *,
    table_name: str,
    base_identity: str,
    base_profile: str | None,
) -> tuple[str, bool]:
    table_id = _find_table_id(
        _list_tables(cli, base_identity=base_identity, base_profile=base_profile),
        table_name,
    )
    if table_id is not None:
        return table_id, False
    _run_lark_cli(
        cli,
        [
            "base",
            "+table-create",
            "--base-token",
            BASE_TOKEN,
            "--name",
            table_name,
            "--fields",
            json.dumps(ARCHIVE_FIELDS, ensure_ascii=False, separators=(",", ":")),
            "--view",
            json.dumps(
                {"name": CURRENT_VIEW_NAME, "type": "grid"},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "--as",
            base_identity,
            "--format",
            "json",
        ],
        expected_identity=base_identity,
        profile=base_profile,
        timeout=120,
    )
    table_id = _find_table_id(
        _list_tables(cli, base_identity=base_identity, base_profile=base_profile),
        table_name,
    )
    if table_id is None:
        raise LarkCliError(f"Lark Base did not expose newly created table {table_name}")
    return table_id, True


def _load_field_ids(
    cli: str,
    *,
    table_id: str,
    base_identity: str,
    base_profile: str | None,
) -> dict[str, str]:
    envelope = _run_lark_cli(
        cli,
        [
            "base",
            "+field-list",
            "--base-token",
            BASE_TOKEN,
            "--table-id",
            table_id,
            "--limit",
            "200",
            "--as",
            base_identity,
            "--format",
            "json",
        ],
        expected_identity=base_identity,
        profile=base_profile,
    )
    data = envelope.get("data")
    fields = data.get("fields") if isinstance(data, Mapping) else None
    if not isinstance(fields, list) or any(not isinstance(item, Mapping) for item in fields):
        raise LarkCliError("Lark Base field list has an unexpected shape")
    expected = {cast(str, item["name"]): cast(str, item["type"]) for item in ARCHIVE_FIELDS}
    by_name: dict[str, Mapping[str, object]] = {}
    for field in cast(list[Mapping[str, object]], fields):
        name = field.get("name")
        if isinstance(name, str) and name in expected:
            if name in by_name:
                raise LarkCliError(f"archive table contains duplicate field {name}")
            by_name[name] = field
    missing = set(expected) - set(by_name)
    if missing:
        raise LarkCliError(f"archive table is missing fields: {','.join(sorted(missing))}")
    ids: dict[str, str] = {}
    for name, expected_type in expected.items():
        field = by_name[name]
        if field.get("type") != expected_type:
            raise LarkCliError(
                f"archive field {name} must be {expected_type}, received {field.get('type')}"
            )
        if name == "法人主体" and field.get("link_table") != MAIN_TABLE_ID:
            raise LarkCliError("archive 法人主体 field must link to the main company table")
        field_id = str(field.get("id") or "").strip()
        if not field_id.startswith("fld"):
            raise LarkCliError(f"archive field {name} has an invalid field ID")
        ids[name] = field_id
    return ids


def _list_views(
    cli: str,
    *,
    table_id: str,
    base_identity: str,
    base_profile: str | None,
) -> list[dict[str, object]]:
    envelope = _run_lark_cli(
        cli,
        [
            "base",
            "+view-list",
            "--base-token",
            BASE_TOKEN,
            "--table-id",
            table_id,
            "--limit",
            "200",
            "--as",
            base_identity,
            "--format",
            "json",
        ],
        expected_identity=base_identity,
        profile=base_profile,
    )
    data = envelope.get("data")
    views = data.get("views") if isinstance(data, Mapping) else None
    if not isinstance(views, list) or any(not isinstance(item, dict) for item in views):
        raise LarkCliError("Lark Base view list has an unexpected shape")
    return cast(list[dict[str, object]], views)


def _ensure_current_view(
    cli: str,
    *,
    table_id: str,
    current_field_id: str,
    base_identity: str,
    base_profile: str | None,
) -> str:
    views = _list_views(
        cli,
        table_id=table_id,
        base_identity=base_identity,
        base_profile=base_profile,
    )
    matches = [str(view.get("id") or "").strip() for view in views if view.get("name") == CURRENT_VIEW_NAME]
    if len(matches) > 1:
        raise LarkCliError(f"archive table contains duplicate view {CURRENT_VIEW_NAME}")
    if not matches:
        _run_lark_cli(
            cli,
            [
                "base",
                "+view-create",
                "--base-token",
                BASE_TOKEN,
                "--table-id",
                table_id,
                "--json",
                json.dumps(
                    {"name": CURRENT_VIEW_NAME, "type": "grid"},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "--as",
                base_identity,
                "--format",
                "json",
            ],
            expected_identity=base_identity,
            profile=base_profile,
        )
        views = _list_views(
            cli,
            table_id=table_id,
            base_identity=base_identity,
            base_profile=base_profile,
        )
        matches = [
            str(view.get("id") or "").strip()
            for view in views
            if view.get("name") == CURRENT_VIEW_NAME
        ]
    if len(matches) != 1 or not matches[0].startswith("vew"):
        raise LarkCliError(f"Lark Base did not expose archive view {CURRENT_VIEW_NAME}")
    view_id = matches[0]
    _run_lark_cli(
        cli,
        [
            "base",
            "+view-set-filter",
            "--base-token",
            BASE_TOKEN,
            "--table-id",
            table_id,
            "--view-id",
            view_id,
            "--json",
            json.dumps(
                {
                    "logic": "and",
                    "conditions": [[current_field_id, "==", True]],
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "--as",
            base_identity,
            "--format",
            "json",
        ],
        expected_identity=base_identity,
        profile=base_profile,
    )
    return view_id


def _load_existing_rows(
    cli: str,
    *,
    table_id: str,
    field_ids: Mapping[str, str],
    base_identity: str,
    base_profile: str | None,
) -> dict[str, ExistingArchiveRow]:
    projected_fields = (
        field_ids["归档唯一键"],
        field_ids["能力代码"],
        field_ids["当前示警"],
    )
    record_ids, rows = _load_projected_records(
        cli,
        table_id=table_id,
        field_ids=projected_fields,
        base_identity=base_identity,
        base_profile=base_profile,
    )
    existing: dict[str, ExistingArchiveRow] = {}
    for record_id, values in zip(record_ids, rows, strict=True):
        raw_key, raw_monitor_code, raw_current = values
        archive_key = raw_key.strip() if isinstance(raw_key, str) else ""
        monitor_code = raw_monitor_code.strip() if isinstance(raw_monitor_code, str) else ""
        if not archive_key:
            continue
        if archive_key in existing:
            raise LarkCliError(f"archive table contains duplicate key {archive_key}")
        if not monitor_code:
            raise LarkCliError(f"archive row {archive_key} is missing monitor code")
        existing[archive_key] = ExistingArchiveRow(
            record_id=record_id,
            archive_key=archive_key,
            monitor_code=monitor_code,
            is_current=raw_current is True,
        )
    return existing


def _format_lark_datetime(value: str) -> str:
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise AlertArchiveError("report generated_at must be an ISO datetime") from error
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _row_values(row: AlertArchiveRow, *, archived_at: str) -> list[object]:
    values: dict[str, object] = {
        "归档唯一键": row.archive_key,
        "检测批次": row.batch_id,
        "法人主体": [{"id": row.company_record_id}],
        "公司代码": row.company_code,
        "公司名称": row.company_name,
        "监测频率": row.cadence,
        "监测期间": row.period,
        "能力代码": row.monitor_code,
        "示警能力": row.monitor_name,
        "检查结论": row.outcome,
        "关键数值": row.key_values,
        "示警具体明细": row.alert_details,
        "证据受限": row.evidence_limited,
        "当前示警": True,
        "来源模式": row.source_mode,
        "报告生成时间": _format_lark_datetime(row.report_generated_at),
        "归档时间": archived_at,
        "驾驶舱链接": row.dashboard_url,
    }
    return [values[name] for name in ARCHIVE_FIELD_NAMES]


def _chunks(values: Sequence[str], *, size: int = BATCH_LIMIT) -> list[list[str]]:
    return [list(values[index : index + size]) for index in range(0, len(values), size)]


@contextmanager
def _json_file_argument(payload: Mapping[str, object]) -> Iterator[str]:
    path = Path.cwd() / f".tax-risk-archive-{uuid4().hex}.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )
    try:
        yield f"@{path.name}"
    finally:
        path.unlink(missing_ok=True)


def _batch_create(
    cli: str,
    *,
    table_id: str,
    field_ids: Mapping[str, str],
    rows: Sequence[AlertArchiveRow],
    archived_at: str,
    base_identity: str,
    base_profile: str | None,
) -> tuple[str, ...]:
    created_ids: list[str] = []
    field_order = [field_ids[name] for name in ARCHIVE_FIELD_NAMES]
    for start in range(0, len(rows), BATCH_LIMIT):
        batch = rows[start : start + BATCH_LIMIT]
        payload: dict[str, object] = {
            "fields": field_order,
            "rows": [_row_values(row, archived_at=archived_at) for row in batch],
        }
        with _json_file_argument(payload) as json_argument:
            envelope = _run_lark_cli(
                cli,
                [
                    "base",
                    "+record-batch-create",
                    "--base-token",
                    BASE_TOKEN,
                    "--table-id",
                    table_id,
                    "--json",
                    json_argument,
                    "--as",
                    base_identity,
                    "--format",
                    "json",
                ],
                expected_identity=base_identity,
                profile=base_profile,
                timeout=120,
            )
        data = envelope.get("data")
        record_ids = data.get("record_id_list") if isinstance(data, Mapping) else None
        if not isinstance(record_ids, list) or len(record_ids) != len(batch):
            raise LarkCliError("Lark Base did not confirm every archived row")
        if any(not isinstance(record_id, str) or not record_id for record_id in record_ids):
            raise LarkCliError("Lark Base returned an invalid archived record ID")
        ignored_fields = data.get("ignored_fields") if isinstance(data, Mapping) else None
        if ignored_fields not in (None, [], {}):
            raise LarkCliError("Lark Base ignored one or more archive fields")
        created_ids.extend(cast(list[str], record_ids))
    return tuple(created_ids)


def _batch_set_current(
    cli: str,
    *,
    table_id: str,
    current_field_id: str,
    record_ids: Sequence[str],
    value: bool,
    base_identity: str,
    base_profile: str | None,
) -> None:
    for batch in _chunks(record_ids):
        _run_lark_cli(
            cli,
            [
                "base",
                "+record-batch-update",
                "--base-token",
                BASE_TOKEN,
                "--table-id",
                table_id,
                "--json",
                json.dumps(
                    {
                        "record_id_list": batch,
                        "patch": {current_field_id: value},
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                "--as",
                base_identity,
                "--format",
                "json",
            ],
            expected_identity=base_identity,
            profile=base_profile,
            timeout=120,
        )


def _archive_table_plan(
    cli: str,
    table_plan: AlertArchiveTablePlan,
    *,
    base_identity: str,
    base_profile: str | None,
    archived_at: str,
) -> ArchiveWriteResult:
    table_id, _ = _ensure_table(
        cli,
        table_name=table_plan.table_name,
        base_identity=base_identity,
        base_profile=base_profile,
    )
    field_ids = _load_field_ids(
        cli,
        table_id=table_id,
        base_identity=base_identity,
        base_profile=base_profile,
    )
    _ensure_current_view(
        cli,
        table_id=table_id,
        current_field_id=field_ids["当前示警"],
        base_identity=base_identity,
        base_profile=base_profile,
    )
    existing = _load_existing_rows(
        cli,
        table_id=table_id,
        field_ids=field_ids,
        base_identity=base_identity,
        base_profile=base_profile,
    )
    desired_keys = {row.archive_key for row in table_plan.rows}
    if len(desired_keys) != len(table_plan.rows):
        raise AlertArchiveError(f"archive plan for {table_plan.table_name} contains duplicate keys")
    new_rows = tuple(row for row in table_plan.rows if row.archive_key not in existing)
    restored_ids = tuple(
        existing[row.archive_key].record_id
        for row in table_plan.rows
        if row.archive_key in existing and not existing[row.archive_key].is_current
    )
    stale_ids = tuple(
        row.record_id
        for key, row in existing.items()
        if row.monitor_code in table_plan.monitor_codes
        and key not in desired_keys
        and row.is_current
    )
    _batch_create(
        cli,
        table_id=table_id,
        field_ids=field_ids,
        rows=new_rows,
        archived_at=archived_at,
        base_identity=base_identity,
        base_profile=base_profile,
    )
    _batch_set_current(
        cli,
        table_id=table_id,
        current_field_id=field_ids["当前示警"],
        record_ids=restored_ids,
        value=True,
        base_identity=base_identity,
        base_profile=base_profile,
    )
    _batch_set_current(
        cli,
        table_id=table_id,
        current_field_id=field_ids["当前示警"],
        record_ids=stale_ids,
        value=False,
        base_identity=base_identity,
        base_profile=base_profile,
    )
    return ArchiveWriteResult(
        table_id=table_id,
        table_name=table_plan.table_name,
        created_rows=len(new_rows),
        restored_rows=len(restored_ids),
        retired_rows=len(stale_ids),
    )


def archive_report(
    report: Mapping[str, object],
    *,
    dashboard_url: str = DEFAULT_DASHBOARD_URL,
    monitor_codes: Collection[str] | None = None,
    base_identity: str = "user",
    base_profile: str | None = None,
    cli: str | None = None,
    execute: bool = True,
) -> tuple[AlertArchivePlan, tuple[ArchiveWriteResult, ...]]:
    executable = cli or shutil.which("lark-cli")
    if executable is None:
        raise LarkCliError("lark-cli is not installed")
    effective_profile = base_profile if base_identity == "bot" else None
    directory = _load_company_directory(
        executable,
        base_identity=base_identity,
        base_profile=effective_profile,
    )
    plan = build_archive_plan(
        report,
        directory,
        dashboard_url=dashboard_url,
        monitor_codes=monitor_codes,
        require_full_scope=True,
    )
    if not execute:
        return plan, ()
    archived_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    results = tuple(
        _archive_table_plan(
            executable,
            table_plan,
            base_identity=base_identity,
            base_profile=effective_profile,
            archived_at=archived_at,
        )
        for table_plan in plan.tables
    )
    return plan, results


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--report",
        type=Path,
        default=REPO_ROOT / "web" / "public" / "real-validation-latest.json",
    )
    parser.add_argument("--dashboard-url", default=DEFAULT_DASHBOARD_URL)
    parser.add_argument("--monitor-code", action="append", dest="monitor_codes")
    parser.add_argument("--base-as", choices=("user", "bot"), default="user")
    parser.add_argument("--base-profile", default=DEFAULT_BASE_PROFILE)
    parser.add_argument("--archive", action="store_true")
    args = parser.parse_args()
    report = _load_json_object(args.report.resolve())
    plan, results = archive_report(
        report,
        dashboard_url=args.dashboard_url,
        monitor_codes=args.monitor_codes,
        base_identity=args.base_as,
        base_profile=args.base_profile,
        execute=args.archive,
    )
    print(f"archive batch: {plan.batch_id}; tables: {len(plan.tables)}")
    for table in plan.tables:
        print(f"planned {table.table_name}: {len(table.rows)} alert rows")
    for result in results:
        print(
            f"archived {result.table_name}: created={result.created_rows}, "
            f"restored={result.restored_rows}, retired={result.retired_rows}"
        )
    if not args.archive:
        print("preview only; add --archive to write Lark Base")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
