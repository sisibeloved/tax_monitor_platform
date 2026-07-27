"""Create execution-scoped Lark Base alert tables with feedback workflows."""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections.abc import Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final, cast
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = REPO_ROOT / "backend"
BACKEND_SRC = REPO_ROOT / "backend" / "src"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from scripts.enqueue_feishu_alert_notifications import (
    BASE_TOKEN,
    DEFAULT_BASE_PROFILE,
    DEFAULT_DASHBOARD_URL,
    MAIN_FINANCE_FIELD_ID,
    MAIN_TABLE_ID,
    QUEUE_PENDING_STATUS,
    QUEUE_STATUS_VALUES,
    LarkCliError,
    _load_company_directory,
    _load_json_object,
    _run_lark_cli,
)
from tax_risk.application.alert_archives import (
    AlertArchiveError,
    AlertArchivePlan,
    AlertArchiveRow,
    AlertArchiveTablePlan,
    build_archive_plan,
)

MAIN_TABLE_NAME: Final[str] = "法人主体所得税税负率&利润率等"
DETAIL_VIEW_NAME: Final[str] = "示警明细"
BATCH_LIMIT: Final[int] = 200

ARCHIVE_FIELDS: Final[tuple[dict[str, object], ...]] = (
    {"type": "text", "name": "驾驶舱链接", "style": {"type": "url"}},
    {
        "type": "datetime",
        "name": "生成时间",
        "style": {"format": "yyyy-MM-dd HH:mm"},
    },
    {"type": "text", "name": "公司名称"},
    {"type": "text", "name": "检查结论"},
    {
        "type": "text",
        "name": "修改的凭证号",
        "description": "业财收到示警后填写实际修改的会计凭证号",
    },
    {"type": "checkbox", "name": "测试推送"},
    {"type": "text", "name": "示警明细"},
    {
        "type": "datetime",
        "name": "提交时间",
        "style": {"format": "yyyy-MM-dd HH:mm"},
    },
    {"type": "text", "name": "失败原因"},
    {
        "type": "link",
        "name": "法人主体",
        "description": "关联法人主体主表，自动化实时读取该记录当前业财",
        "link_table": MAIN_TABLE_ID,
        "bidirectional": False,
    },
    {"type": "text", "name": "监测期间"},
    {"type": "text", "name": "关键数值"},
    {"type": "text", "name": "公司代码"},
    {
        "type": "checkbox",
        "name": "推送",
        "description": "勾选后手动向当前法人主体的最新业财发送本行示警",
    },
    {"type": "text", "name": "示警能力"},
    {
        "type": "text",
        "name": "推送唯一键",
        "description": "平台生成的幂等键，防止同一示警重复推送",
    },
    {
        "type": "select",
        "name": "推送状态",
        "multiple": False,
        "options": [
            {"name": "待推送", "hue": "Yellow", "lightness": "Lighter"},
            {"name": "已提交", "hue": "Green", "lightness": "Lighter"},
            {"name": "已跳过", "hue": "Gray", "lightness": "Lighter"},
            {"name": "失败", "hue": "Red", "lightness": "Lighter"},
        ],
        "default_value": [QUEUE_PENDING_STATUS],
    },
    {
        "type": "text",
        "name": "修改的会计年度",
        "description": "业财收到示警后填写修改凭证所属会计年度（YYYY）",
    },
    {
        "type": "lookup",
        "name": "业财姓名",
        "description": "通过法人主体关联实时显示主表当前业财人员",
        "from": MAIN_TABLE_NAME,
        "select": MAIN_FINANCE_FIELD_ID,
        "where": {
            "logic": "and",
            "conditions": [["公司代码", "==", {"type": "field_ref", "field": "公司代码"}]],
        },
        "aggregate": "raw_value",
    },
)

ARCHIVE_FIELD_NAMES: Final[tuple[str, ...]] = tuple(
    cast(str, field["name"]) for field in ARCHIVE_FIELDS
)
WRITABLE_ARCHIVE_FIELD_NAMES: Final[tuple[str, ...]] = tuple(
    name for name in ARCHIVE_FIELD_NAMES if name != "业财姓名"
)


@dataclass(frozen=True, slots=True)
class ArchiveWriteResult:
    table_id: str
    table_name: str
    created_rows: int
    workflow_id: str | None


def _list_tables(
    cli: str,
    *,
    base_identity: str,
    base_profile: str | None,
) -> list[dict[str, object]]:
    tables: list[dict[str, object]] = []
    offset = 0
    while True:
        envelope = _run_lark_cli(
            cli,
            [
                "base",
                "+table-list",
                "--base-token",
                BASE_TOKEN,
                "--offset",
                str(offset),
                "--limit",
                "100",
                "--as",
                base_identity,
            ],
            expected_identity=base_identity,
            profile=base_profile,
        )
        data = envelope.get("data")
        page = data.get("tables") if isinstance(data, Mapping) else None
        if not isinstance(page, list) or any(not isinstance(item, dict) for item in page):
            raise LarkCliError("Lark Base table list has an unexpected shape")
        tables.extend(cast(list[dict[str, object]], page))
        total = data.get("total") if isinstance(data, Mapping) else None
        if isinstance(total, int) and len(tables) >= total:
            return tables
        if len(page) < 100:
            return tables
        offset += len(page)


def _execution_table_name(
    tables: Sequence[Mapping[str, object]],
    requested_name: str,
    *,
    executed_at: str,
) -> str:
    names = {
        str(table.get("name") or "").strip()
        for table in tables
        if isinstance(table.get("name"), str)
    }
    if requested_name not in names:
        return requested_name
    try:
        parsed = datetime.strptime(executed_at, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=datetime.now().astimezone().tzinfo
        )
        suffix = parsed.strftime("%Y%m%d-%H%M%S")
    except ValueError as error:
        raise AlertArchiveError("execution time must use YYYY-MM-DD HH:mm:ss") from error
    candidate = f"{requested_name}-{suffix}"
    sequence = 2
    while candidate in names:
        candidate = f"{requested_name}-{suffix}-{sequence}"
        sequence += 1
    return candidate


def _created_table_id(envelope: Mapping[str, object]) -> str | None:
    data = envelope.get("data")
    table = data.get("table") if isinstance(data, Mapping) else None
    if not isinstance(table, Mapping):
        return None
    table_id = str(table.get("table_id") or table.get("id") or "").strip()
    return table_id if table_id.startswith("tbl") else None


def _create_execution_table(
    cli: str,
    *,
    requested_name: str,
    executed_at: str,
    base_identity: str,
    base_profile: str | None,
) -> tuple[str, str]:
    table_name = _execution_table_name(
        _list_tables(cli, base_identity=base_identity, base_profile=base_profile),
        requested_name,
        executed_at=executed_at,
    )
    envelope = _run_lark_cli(
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
                {"name": DETAIL_VIEW_NAME, "type": "grid"},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            "--as",
            base_identity,
        ],
        expected_identity=base_identity,
        profile=base_profile,
        timeout=120,
    )
    table_id = _created_table_id(envelope)
    if table_id is None:
        matches = [
            str(table.get("id") or "").strip()
            for table in _list_tables(
                cli,
                base_identity=base_identity,
                base_profile=base_profile,
            )
            if table.get("name") == table_name
        ]
        if len(matches) != 1 or not matches[0].startswith("tbl"):
            raise LarkCliError(f"Lark Base did not expose newly created table {table_name}")
        table_id = matches[0]
    return table_id, table_name


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
            "--offset",
            "0",
            "--limit",
            "200",
            "--as",
            base_identity,
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
        if isinstance(name, str):
            if name in by_name:
                raise LarkCliError(f"detail table contains duplicate field {name}")
            by_name[name] = field
    missing = set(expected) - set(by_name)
    unexpected = set(by_name) - set(expected)
    if missing or unexpected:
        raise LarkCliError(
            "detail table fields differ from 示警推送队列: "
            f"missing={','.join(sorted(missing)) or '-'}; "
            f"unexpected={','.join(sorted(unexpected)) or '-'}"
        )
    ids: dict[str, str] = {}
    for name, expected_type in expected.items():
        field = by_name[name]
        if field.get("type") != expected_type:
            raise LarkCliError(
                f"detail field {name} must be {expected_type}, received {field.get('type')}"
            )
        field_id = str(field.get("id") or "").strip()
        if not field_id.startswith("fld"):
            raise LarkCliError(f"detail field {name} has an invalid field ID")
        ids[name] = field_id
    if by_name["法人主体"].get("link_table") != MAIN_TABLE_ID:
        raise LarkCliError("detail 法人主体 field must link to the main company table")
    finance_field = by_name["业财姓名"]
    if finance_field.get("from") != MAIN_TABLE_NAME:
        raise LarkCliError("detail 业财姓名 lookup must read the main company table")
    if finance_field.get("select") != MAIN_FINANCE_FIELD_ID:
        raise LarkCliError("detail 业财姓名 lookup must read the main-table 业财 field")
    dashboard_style = by_name["驾驶舱链接"].get("style")
    if not isinstance(dashboard_style, Mapping) or dashboard_style.get("type") != "url":
        raise LarkCliError("detail 驾驶舱链接 must use URL style")
    for name in ("生成时间", "提交时间"):
        style = by_name[name].get("style")
        if not isinstance(style, Mapping) or style.get("format") != "yyyy-MM-dd HH:mm":
            raise LarkCliError(f"detail {name} must use yyyy-MM-dd HH:mm format")
    status_field = by_name["推送状态"]
    options = status_field.get("options")
    if not isinstance(options, list):
        raise LarkCliError("detail 推送状态 options are missing")
    option_names = {
        option.get("name")
        for option in options
        if isinstance(option, Mapping) and isinstance(option.get("name"), str)
    }
    if option_names != QUEUE_STATUS_VALUES or len(options) != len(QUEUE_STATUS_VALUES):
        raise LarkCliError("detail 推送状态 options differ from 示警推送队列")
    if status_field.get("multiple") is not False:
        raise LarkCliError("detail 推送状态 must be single-select")
    if status_field.get("default_value") != [QUEUE_PENDING_STATUS]:
        raise LarkCliError("detail 推送状态 must default to 待推送")
    return ids


def _format_lark_datetime(value: str) -> str:
    normalized = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise AlertArchiveError("report generated_at must be an ISO datetime") from error
    return parsed.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def _row_values(row: AlertArchiveRow) -> list[object]:
    values: dict[str, object] = {
        "驾驶舱链接": row.dashboard_url,
        "生成时间": _format_lark_datetime(row.report_generated_at),
        "公司名称": row.company_name,
        "检查结论": row.outcome,
        "修改的凭证号": None,
        "测试推送": False,
        "示警明细": row.alert_details,
        "提交时间": None,
        "失败原因": None,
        "法人主体": [{"id": row.company_record_id}],
        "监测期间": row.period,
        "关键数值": row.key_values,
        "公司代码": row.company_code,
        "推送": False,
        "示警能力": row.monitor_name,
        "推送唯一键": row.archive_key,
        "推送状态": QUEUE_PENDING_STATUS,
        "修改的会计年度": None,
    }
    return [values[name] for name in WRITABLE_ARCHIVE_FIELD_NAMES]


@contextmanager
def _json_file_argument(payload: Mapping[str, object]) -> Iterator[str]:
    path = Path.cwd() / f".tax-risk-detail-{uuid4().hex}.json"
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
    base_identity: str,
    base_profile: str | None,
) -> tuple[str, ...]:
    created_ids: list[str] = []
    field_order = [field_ids[name] for name in WRITABLE_ARCHIVE_FIELD_NAMES]
    for start in range(0, len(rows), BATCH_LIMIT):
        batch = rows[start : start + BATCH_LIMIT]
        payload: dict[str, object] = {
            "fields": field_order,
            "rows": [_row_values(row) for row in batch],
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
                ],
                expected_identity=base_identity,
                profile=base_profile,
                timeout=120,
            )
        data = envelope.get("data")
        record_ids = data.get("record_id_list") if isinstance(data, Mapping) else None
        if not isinstance(record_ids, list) or len(record_ids) != len(batch):
            raise LarkCliError("Lark Base did not confirm every detail row")
        if any(not isinstance(record_id, str) or not record_id for record_id in record_ids):
            raise LarkCliError("Lark Base returned an invalid detail record ID")
        ignored_fields = data.get("ignored_fields") if isinstance(data, Mapping) else None
        if ignored_fields not in (None, [], {}):
            raise LarkCliError("Lark Base ignored one or more detail fields")
        created_ids.extend(cast(list[str], record_ids))
    return tuple(created_ids)


def _feedback_workflow_definition(
    table_name: str,
    field_ids: Mapping[str, str],
) -> dict[str, object]:
    trigger_id = "step_manual_push"

    def ref(field_name: str) -> dict[str, str]:
        return {
            "value_type": "ref",
            "value": f"$.{trigger_id}.{field_ids[field_name]}",
        }

    return {
        "title": f"{table_name}-示警明细手动推送",
        "steps": [
            {
                "id": trigger_id,
                "type": "SetRecordTrigger",
                "title": "勾选本行推送时触发",
                "next": "step_send_alert",
                "data": {
                    "table_name": table_name,
                    "field_watch_info": [
                        {
                            "field_name": "推送",
                            "operator": "is",
                            "value": [{"value_type": "boolean", "value": True}],
                        }
                    ],
                    "trigger_control_list": [
                        "pasteUpdate",
                        "automationBatchUpdate",
                        "appendImport",
                        "openAPIBatchUpdate",
                    ],
                },
            },
            {
                "id": "step_send_alert",
                "type": "LarkMessageAction",
                "title": "发送示警给当前业财",
                "next": "step_mark_submitted",
                "data": {
                    "receiver": [
                        {
                            "value_type": "ref",
                            "value": (
                                f"$.{trigger_id}.{field_ids['法人主体']}.{MAIN_FINANCE_FIELD_ID}"
                            ),
                        }
                    ],
                    "send_to_everyone": False,
                    "title": [
                        {"value_type": "text", "value": "[税务风险示警] "},
                        ref("公司名称"),
                        {"value_type": "text", "value": " - "},
                        ref("示警能力"),
                    ],
                    "content": [
                        {"value_type": "text", "value": "公司代码："},
                        ref("公司代码"),
                        {"value_type": "text", "value": "\n监测期间："},
                        ref("监测期间"),
                        {"value_type": "text", "value": "\n检查结论："},
                        ref("检查结论"),
                        {"value_type": "text", "value": "\n关键数值："},
                        ref("关键数值"),
                        {"value_type": "text", "value": "\n示警明细："},
                        ref("示警明细"),
                        {
                            "value_type": "text",
                            "value": (
                                "\n\n整改反馈：请点击“填写整改信息”，填写“修改的凭证号”"
                                "和“修改的会计年度”。"
                            ),
                        },
                    ],
                    "btn_list": [
                        {
                            "text": "查看驾驶舱",
                            "btn_action": "openLink",
                            "link": [ref("驾驶舱链接")],
                        },
                        {
                            "text": "填写整改信息",
                            "btn_action": "openLink",
                            "link": [
                                {
                                    "value_type": "ref",
                                    "value": f"$.{trigger_id}.recordLink",
                                }
                            ],
                        },
                    ],
                },
            },
            {
                "id": "step_mark_submitted",
                "type": "SetRecordAction",
                "title": "标记为已提交并清空推送勾选",
                "next": None,
                "data": {
                    "table_name": table_name,
                    "max_set_record_num": 1,
                    "field_values": [
                        {
                            "field_name": "推送状态",
                            "value": [
                                {
                                    "value_type": "option",
                                    "value": {"name": "已提交"},
                                }
                            ],
                        },
                        {
                            "field_name": "提交时间",
                            "value": [
                                {
                                    "value_type": "ref",
                                    "value": f"$.{trigger_id}.startTime",
                                }
                            ],
                        },
                        {
                            "field_name": "推送",
                            "value": [{"value_type": "boolean", "value": False}],
                        },
                    ],
                    "filter_info": None,
                    "ref_info": {"step_id": trigger_id},
                },
            },
        ],
    }


def _create_feedback_workflow(
    cli: str,
    *,
    table_id: str,
    table_name: str,
    field_ids: Mapping[str, str],
    base_identity: str,
    base_profile: str | None,
) -> str:
    payload = _feedback_workflow_definition(table_name, field_ids)
    payload["client_token"] = f"taxrisk-{table_id}"
    envelope = _run_lark_cli(
        cli,
        [
            "base",
            "+workflow-create",
            "--base-token",
            BASE_TOKEN,
            "--json",
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            "--as",
            base_identity,
        ],
        expected_identity=base_identity,
        profile=base_profile,
        timeout=120,
    )
    data = envelope.get("data")
    workflow_id = str(data.get("workflow_id") or "").strip() if isinstance(data, Mapping) else ""
    if not workflow_id.startswith("wkf"):
        raise LarkCliError("Lark Base did not return the detail workflow ID")
    enabled = _run_lark_cli(
        cli,
        [
            "base",
            "+workflow-enable",
            "--base-token",
            BASE_TOKEN,
            "--workflow-id",
            workflow_id,
            "--as",
            base_identity,
        ],
        expected_identity=base_identity,
        profile=base_profile,
        timeout=120,
    )
    enabled_data = enabled.get("data")
    if not isinstance(enabled_data, Mapping) or enabled_data.get("status") != "enabled":
        raise LarkCliError(f"Lark Base did not enable detail workflow {workflow_id}")
    return workflow_id


def _archive_table_plan(
    cli: str,
    table_plan: AlertArchiveTablePlan,
    *,
    base_identity: str,
    base_profile: str | None,
    executed_at: str,
    create_workflow: bool,
) -> ArchiveWriteResult:
    desired_keys = {row.archive_key for row in table_plan.rows}
    if len(desired_keys) != len(table_plan.rows):
        raise AlertArchiveError(f"detail plan for {table_plan.table_name} contains duplicate keys")
    table_id, table_name = _create_execution_table(
        cli,
        requested_name=table_plan.table_name,
        executed_at=executed_at,
        base_identity=base_identity,
        base_profile=base_profile,
    )
    field_ids = _load_field_ids(
        cli,
        table_id=table_id,
        base_identity=base_identity,
        base_profile=base_profile,
    )
    record_ids = _batch_create(
        cli,
        table_id=table_id,
        field_ids=field_ids,
        rows=table_plan.rows,
        base_identity=base_identity,
        base_profile=base_profile,
    )
    workflow_id = None
    if create_workflow:
        workflow_id = _create_feedback_workflow(
            cli,
            table_id=table_id,
            table_name=table_name,
            field_ids=field_ids,
            base_identity=base_identity,
            base_profile=base_profile,
        )
    return ArchiveWriteResult(
        table_id=table_id,
        table_name=table_name,
        created_rows=len(record_ids),
        workflow_id=workflow_id,
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
    create_workflow: bool = True,
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
    executed_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    results = tuple(
        _archive_table_plan(
            executable,
            table_plan,
            base_identity=base_identity,
            base_profile=effective_profile,
            executed_at=executed_at,
            create_workflow=create_workflow,
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
    parser.add_argument("--skip-workflow", action="store_true")
    args = parser.parse_args()
    report = _load_json_object(args.report.resolve())
    plan, results = archive_report(
        report,
        dashboard_url=args.dashboard_url,
        monitor_codes=args.monitor_codes,
        base_identity=args.base_as,
        base_profile=args.base_profile,
        execute=args.archive,
        create_workflow=not args.skip_workflow,
    )
    print(f"detail batch: {plan.batch_id}; tables: {len(plan.tables)}")
    for table in plan.tables:
        print(f"planned {table.table_name}: {len(table.rows)} alert rows")
    for result in results:
        print(
            f"created {result.table_name}: rows={result.created_rows}, "
            f"workflow={result.workflow_id or 'skipped'}"
        )
    if not args.archive:
        print("preview only; add --archive to write Lark Base")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
