"""Preview or enqueue manually triggered Feishu alert records in Lark Base."""

from __future__ import annotations

import argparse
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Final, cast


REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_SRC = REPO_ROOT / "backend" / "src"
if str(BACKEND_SRC) not in sys.path:
    sys.path.insert(0, str(BACKEND_SRC))

from tax_risk.application.alert_notifications import (  # noqa: E402
    AlertNotificationError,
    AlertQueueItem,
    AlertQueuePlan,
    CompanyLinkDirectory,
    build_company_link_directory,
    build_queue_plan,
)


BASE_TOKEN: Final[str] = "A1Kwb4tkZaZdE2s3C2dcG49Fn2d"
MAIN_TABLE_ID: Final[str] = "tbl4PCNdcl4BYzgZ"
QUEUE_TABLE_ID: Final[str] = "tblUPRyqDLPTR4vv"
DEFAULT_BASE_PROFILE: Final[str] = "tax-risk-notifier"
DEFAULT_DASHBOARD_URL: Final[str] = "https://hailiang.aiforce.cloud/app/app_17agcfby4jx"

COMPANY_CODE_FIELD_ID: Final[str] = "fld5uBjB9R"
COMPANY_NAME_FIELD_ID: Final[str] = "fld65JDObx"
MAIN_FIELD_IDS: Final[tuple[str, ...]] = (
    COMPANY_CODE_FIELD_ID,
    COMPANY_NAME_FIELD_ID,
)

QUEUE_UNIQUE_KEY_FIELD_ID: Final[str] = "fld04jdfl3"
QUEUE_COMPANY_LINK_FIELD_ID: Final[str] = "fldpt99uls"
QUEUE_COMPANY_CODE_FIELD_ID: Final[str] = "fld1zobL1o"
QUEUE_COMPANY_NAME_FIELD_ID: Final[str] = "fldoVoUdwI"
QUEUE_PERIOD_FIELD_ID: Final[str] = "fld06jo2wH"
QUEUE_MONITOR_FIELD_ID: Final[str] = "fldHdCQwD5"
QUEUE_OUTCOME_FIELD_ID: Final[str] = "fldSar5I6n"
QUEUE_KEY_VALUES_FIELD_ID: Final[str] = "fldcJopw77"
QUEUE_DETAILS_FIELD_ID: Final[str] = "fldR6wt7V1"
QUEUE_DASHBOARD_URL_FIELD_ID: Final[str] = "fldiiMZDVW"
QUEUE_STATUS_FIELD_ID: Final[str] = "fldQqqrPAT"
QUEUE_PUSH_FIELD_ID: Final[str] = "fldJU3PyLk"
QUEUE_TEST_PUSH_FIELD_ID: Final[str] = "flddcbIkbl"
QUEUE_GENERATED_AT_FIELD_ID: Final[str] = "fldVkuGJE4"
QUEUE_SUBMITTED_AT_FIELD_ID: Final[str] = "fldrBQi6ui"
QUEUE_FAILURE_REASON_FIELD_ID: Final[str] = "fldfnKtD3j"
QUEUE_FINANCE_NAME_FIELD_ID: Final[str] = "fldVuyd9If"
QUEUE_MODIFIED_VOUCHER_NUMBER_FIELD_ID: Final[str] = "fldCy6TW1d"
QUEUE_MODIFIED_ACCOUNTING_YEAR_FIELD_ID: Final[str] = "fldrzAPx9X"
MAIN_FINANCE_FIELD_ID: Final[str] = "fld2f8VqpE"
QUEUE_EXISTING_FIELD_IDS: Final[tuple[str, ...]] = (
    QUEUE_UNIQUE_KEY_FIELD_ID,
    QUEUE_STATUS_FIELD_ID,
)
QUEUE_PENDING_STATUS: Final[str] = "待推送"
QUEUE_STATUS_VALUES: Final[frozenset[str]] = frozenset({"待推送", "已提交", "已跳过", "失败"})
QUEUE_FIELD_CONTRACT: Final[tuple[tuple[str, str, str], ...]] = (
    (QUEUE_COMPANY_NAME_FIELD_ID, "公司名称", "text"),
    (QUEUE_UNIQUE_KEY_FIELD_ID, "推送唯一键", "text"),
    (QUEUE_SUBMITTED_AT_FIELD_ID, "提交时间", "datetime"),
    (QUEUE_PERIOD_FIELD_ID, "监测期间", "text"),
    (QUEUE_MONITOR_FIELD_ID, "示警能力", "text"),
    (QUEUE_FAILURE_REASON_FIELD_ID, "失败原因", "text"),
    (QUEUE_STATUS_FIELD_ID, "推送状态", "select"),
    (QUEUE_TEST_PUSH_FIELD_ID, "测试推送", "checkbox"),
    (QUEUE_COMPANY_LINK_FIELD_ID, "法人主体", "link"),
    (QUEUE_GENERATED_AT_FIELD_ID, "生成时间", "datetime"),
    (QUEUE_OUTCOME_FIELD_ID, "检查结论", "text"),
    (QUEUE_FINANCE_NAME_FIELD_ID, "业财姓名", "lookup"),
    (QUEUE_PUSH_FIELD_ID, "推送", "checkbox"),
    (QUEUE_KEY_VALUES_FIELD_ID, "关键数值", "text"),
    (QUEUE_DASHBOARD_URL_FIELD_ID, "驾驶舱链接", "text"),
    (QUEUE_COMPANY_CODE_FIELD_ID, "公司代码", "text"),
    (QUEUE_DETAILS_FIELD_ID, "示警明细", "text"),
    (QUEUE_MODIFIED_VOUCHER_NUMBER_FIELD_ID, "修改的凭证号", "text"),
    (QUEUE_MODIFIED_ACCOUNTING_YEAR_FIELD_ID, "修改的会计年度", "text"),
)


class LarkCliError(RuntimeError):
    """Credential-safe wrapper for failed lark-cli operations."""


@dataclass(frozen=True, slots=True)
class QueueWriteResult:
    created_rows: int
    existing_rows: int
    record_ids: tuple[str, ...]


def _load_json_object(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AlertNotificationError(f"cannot read JSON object from {path}") from error
    if not isinstance(payload, dict):
        raise AlertNotificationError(f"{path} must contain a JSON object")
    return cast(dict[str, object], payload)


def _run_lark_cli(
    cli: str,
    arguments: list[str],
    *,
    timeout: float = 60.0,
    expected_identity: str | None = None,
    profile: str | None = None,
) -> dict[str, object]:
    environment = os.environ.copy()
    environment["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
    environment["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
    try:
        command = [cli]
        if profile is not None:
            command.extend(("--profile", profile))
        command.extend(arguments)
        completed = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout,
            env=environment,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LarkCliError("lark-cli could not complete the requested operation") from error
    raw_output = completed.stdout.strip() or completed.stderr.strip()
    try:
        envelope = json.loads(raw_output)
    except json.JSONDecodeError as error:
        raise LarkCliError("lark-cli returned an invalid JSON response") from error
    if (
        completed.returncode != 0
        or not isinstance(envelope, dict)
        or envelope.get("ok") is not True
    ):
        error_payload = envelope.get("error") if isinstance(envelope, dict) else None
        error_type = "unknown"
        error_subtype = "unknown"
        safe_details: list[str] = []
        if isinstance(error_payload, dict):
            error_type = str(error_payload.get("type") or "unknown")
            error_subtype = str(error_payload.get("subtype") or "unknown")
            for key in ("code", "message", "hint", "missing_scopes", "console_url"):
                value = error_payload.get(key)
                if value not in (None, "", [], {}):
                    safe_details.append(f"{key}={value}")
        suffix = f": {'; '.join(safe_details)}" if safe_details else ""
        raise LarkCliError(f"lark-cli failed ({error_type}/{error_subtype}){suffix}")
    if expected_identity is not None and envelope.get("identity") != expected_identity:
        raise LarkCliError(
            "lark-cli used an unexpected identity "
            f"(expected {expected_identity}, received {envelope.get('identity') or 'unknown'})"
        )
    return cast(dict[str, object], envelope)


def _load_projected_records(
    cli: str,
    *,
    table_id: str,
    field_ids: Sequence[str],
    base_identity: str,
    base_profile: str | None,
) -> tuple[list[str], list[list[object]]]:
    record_ids: list[str] = []
    rows: list[list[object]] = []
    offset = 0
    while True:
        arguments = [
            "base",
            "+record-list",
            "--base-token",
            BASE_TOKEN,
            "--table-id",
            table_id,
        ]
        for field_id in field_ids:
            arguments.extend(("--field-id", field_id))
        arguments.extend(
            (
                "--offset",
                str(offset),
                "--limit",
                "200",
                "--format",
                "json",
                "--as",
                base_identity,
            )
        )
        envelope = _run_lark_cli(
            cli,
            arguments,
            expected_identity=base_identity,
            profile=base_profile,
        )
        data = envelope.get("data")
        if not isinstance(data, dict) or data.get("field_id_list") != list(field_ids):
            raise LarkCliError("Lark Base projection contract changed")
        page_rows = data.get("data")
        page_record_ids = data.get("record_id_list")
        if not isinstance(page_rows, list) or any(
            not isinstance(row, list) or len(row) != len(field_ids) for row in page_rows
        ):
            raise LarkCliError("Lark Base projected rows have an unexpected shape")
        if not isinstance(page_record_ids, list) or len(page_record_ids) != len(page_rows):
            raise LarkCliError("Lark Base projected rows are missing record IDs")
        if any(not isinstance(record_id, str) or not record_id for record_id in page_record_ids):
            raise LarkCliError("Lark Base returned an invalid record ID")
        rows.extend(cast(list[list[object]], page_rows))
        record_ids.extend(cast(list[str], page_record_ids))
        if data.get("has_more") is not True:
            return record_ids, rows
        if not page_rows:
            raise LarkCliError("Lark Base pagination did not advance")
        offset += len(page_rows)


def _load_company_directory(
    cli: str,
    *,
    base_identity: str,
    base_profile: str | None,
) -> CompanyLinkDirectory:
    record_ids, rows = _load_projected_records(
        cli,
        table_id=MAIN_TABLE_ID,
        field_ids=MAIN_FIELD_IDS,
        base_identity=base_identity,
        base_profile=base_profile,
    )
    return build_company_link_directory(record_ids, rows)


def _validate_queue_schema(
    cli: str,
    *,
    base_identity: str,
    base_profile: str | None,
) -> None:
    envelope = _run_lark_cli(
        cli,
        [
            "base",
            "+field-list",
            "--base-token",
            BASE_TOKEN,
            "--table-id",
            QUEUE_TABLE_ID,
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
        raise LarkCliError("Lark Base queue field list has an unexpected shape")
    by_id: dict[str, Mapping[str, object]] = {}
    for raw_field in fields:
        field = cast(Mapping[str, object], raw_field)
        field_id = field.get("id")
        if isinstance(field_id, str):
            if field_id in by_id:
                raise LarkCliError(f"Lark Base queue contains duplicate field ID {field_id}")
            by_id[field_id] = field
    for field_id, expected_name, expected_type in QUEUE_FIELD_CONTRACT:
        field = by_id.get(field_id)
        if field is None:
            raise LarkCliError(f"Lark Base queue is missing field {expected_name} ({field_id})")
        if field.get("name") != expected_name or field.get("type") != expected_type:
            raise LarkCliError(
                f"Lark Base queue field {field_id} must be {expected_name}/{expected_type}, "
                f"received {field.get('name')}/{field.get('type')}"
            )
    company_field = by_id[QUEUE_COMPANY_LINK_FIELD_ID]
    if company_field.get("link_table") != MAIN_TABLE_ID:
        raise LarkCliError("Lark Base queue 法人主体 field must link to the main company table")
    finance_field = by_id[QUEUE_FINANCE_NAME_FIELD_ID]
    if finance_field.get("select") != MAIN_FINANCE_FIELD_ID:
        raise LarkCliError("Lark Base queue 业财姓名 lookup must read the main-table 业财 field")
    dashboard_style = by_id[QUEUE_DASHBOARD_URL_FIELD_ID].get("style")
    if not isinstance(dashboard_style, Mapping) or dashboard_style.get("type") != "url":
        raise LarkCliError("Lark Base queue 驾驶舱链接 must use URL style")
    for datetime_field_id, datetime_field_name in (
        (QUEUE_GENERATED_AT_FIELD_ID, "生成时间"),
        (QUEUE_SUBMITTED_AT_FIELD_ID, "提交时间"),
    ):
        datetime_style = by_id[datetime_field_id].get("style")
        if (
            not isinstance(datetime_style, Mapping)
            or datetime_style.get("format") != "yyyy-MM-dd HH:mm"
        ):
            raise LarkCliError(
                f"Lark Base queue {datetime_field_name} must use yyyy-MM-dd HH:mm format"
            )
    status_field = by_id[QUEUE_STATUS_FIELD_ID]
    raw_options = status_field.get("options")
    if not isinstance(raw_options, list):
        raise LarkCliError("Lark Base queue 推送状态 options are missing")
    status_values = {
        option.get("name")
        for option in raw_options
        if isinstance(option, Mapping) and isinstance(option.get("name"), str)
    }
    if status_values != QUEUE_STATUS_VALUES or len(raw_options) != len(QUEUE_STATUS_VALUES):
        raise LarkCliError("Lark Base queue 推送状态 options do not match the feedback workflow")
    if status_field.get("multiple") is not False:
        raise LarkCliError("Lark Base queue 推送状态 must be single-select")
    if status_field.get("default_value") != [QUEUE_PENDING_STATUS]:
        raise LarkCliError("Lark Base queue 推送状态 must default to 待推送")


def _load_existing_queue_keys(
    cli: str,
    *,
    base_identity: str,
    base_profile: str | None,
) -> dict[str, tuple[str, str | None]]:
    record_ids, rows = _load_projected_records(
        cli,
        table_id=QUEUE_TABLE_ID,
        field_ids=QUEUE_EXISTING_FIELD_IDS,
        base_identity=base_identity,
        base_profile=base_profile,
    )
    existing: dict[str, tuple[str, str | None]] = {}
    for record_id, row in zip(record_ids, rows, strict=True):
        raw_key, raw_status = row
        key = raw_key.strip() if isinstance(raw_key, str) else ""
        if not key:
            continue
        if key in existing:
            raise LarkCliError(f"Lark Base queue contains duplicate idempotency key {key}")
        status = raw_status.strip() if isinstance(raw_status, str) and raw_status.strip() else None
        existing[key] = (record_id, status)
    return existing


def _queue_record_fields(item: AlertQueueItem, *, generated_at: str) -> dict[str, object]:
    return {
        QUEUE_UNIQUE_KEY_FIELD_ID: item.idempotency_key,
        QUEUE_COMPANY_LINK_FIELD_ID: [{"id": item.company_record_id}],
        QUEUE_COMPANY_CODE_FIELD_ID: item.company_code,
        QUEUE_COMPANY_NAME_FIELD_ID: item.company_name,
        QUEUE_PERIOD_FIELD_ID: item.period,
        QUEUE_MONITOR_FIELD_ID: item.monitor_name,
        QUEUE_OUTCOME_FIELD_ID: item.outcome,
        QUEUE_KEY_VALUES_FIELD_ID: item.key_values,
        QUEUE_DETAILS_FIELD_ID: item.alert_details,
        QUEUE_DASHBOARD_URL_FIELD_ID: item.dashboard_url,
        QUEUE_STATUS_FIELD_ID: QUEUE_PENDING_STATUS,
        QUEUE_PUSH_FIELD_ID: False,
        QUEUE_TEST_PUSH_FIELD_ID: item.test_push,
        QUEUE_GENERATED_AT_FIELD_ID: generated_at,
    }


def _create_queue_record(
    cli: str,
    item: AlertQueueItem,
    *,
    generated_at: str,
    base_identity: str,
    base_profile: str | None,
) -> str:
    envelope = _run_lark_cli(
        cli,
        [
            "base",
            "+record-upsert",
            "--base-token",
            BASE_TOKEN,
            "--table-id",
            QUEUE_TABLE_ID,
            "--json",
            json.dumps(
                _queue_record_fields(item, generated_at=generated_at),
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
    data = envelope.get("data")
    if not isinstance(data, dict) or data.get("created") is not True:
        raise LarkCliError("Lark Base did not confirm queue record creation")
    record = data.get("record")
    if not isinstance(record, dict):
        raise LarkCliError("Lark Base queue creation response is missing the record")
    record_id = str(
        record.get("record_id") or record.get("id") or record.get("_record_id") or ""
    ).strip()
    ignored_fields = data.get("ignored_fields")
    if ignored_fields not in (None, [], {}):
        raise LarkCliError("Lark Base ignored one or more queue fields")
    if not record_id:
        created_record = _load_existing_queue_keys(
            cli,
            base_identity=base_identity,
            base_profile=base_profile,
        ).get(item.idempotency_key)
        if created_record is None:
            raise LarkCliError(
                "Lark Base queue creation response omitted record_id and the created key "
                "could not be confirmed"
            )
        record_id = created_record[0]
    return record_id


def _enqueue_plan(
    cli: str,
    plan: AlertQueuePlan,
    *,
    base_identity: str,
    base_profile: str | None,
) -> tuple[int, int, tuple[str, ...]]:
    existing = _load_existing_queue_keys(
        cli,
        base_identity=base_identity,
        base_profile=base_profile,
    )
    generated_at = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")
    created = 0
    already_queued = 0
    record_ids: list[str] = []
    for item in plan.items:
        existing_entry = existing.get(item.idempotency_key)
        if existing_entry is not None:
            already_queued += 1
            record_ids.append(existing_entry[0])
            continue
        record_id = _create_queue_record(
            cli,
            item,
            generated_at=generated_at,
            base_identity=base_identity,
            base_profile=base_profile,
        )
        existing[item.idempotency_key] = (record_id, QUEUE_PENDING_STATUS)
        record_ids.append(record_id)
        created += 1
    return created, already_queued, tuple(record_ids)


def enqueue_report(
    report: Mapping[str, object],
    *,
    monitor_codes: Collection[str] | None = None,
    max_companies_per_monitor: int = 3,
    dashboard_url: str = DEFAULT_DASHBOARD_URL,
    test_push: bool = False,
    base_identity: str = "bot",
    base_profile: str | None = DEFAULT_BASE_PROFILE,
    cli: str | None = None,
    execute: bool = True,
) -> tuple[AlertQueuePlan, QueueWriteResult | None]:
    """Plan and optionally enqueue report alerts using the verified feedback schema."""

    executable = cli or shutil.which("lark-cli")
    if executable is None:
        raise LarkCliError("lark-cli is not installed")
    effective_profile = base_profile if base_identity == "bot" else None
    directory = _load_company_directory(
        executable,
        base_identity=base_identity,
        base_profile=effective_profile,
    )
    plan = build_queue_plan(
        report,
        directory,
        max_companies_per_monitor=max_companies_per_monitor,
        dashboard_url=dashboard_url,
        test_push=test_push,
        monitor_codes=monitor_codes,
    )
    if plan.skipped:
        skipped_keys = ",".join(f"{item.company_code}:{item.monitor_code}" for item in plan.skipped)
        raise AlertNotificationError(
            f"queue plan cannot resolve every company relationship: {skipped_keys}"
        )
    if not execute:
        return plan, None
    _validate_queue_schema(
        executable,
        base_identity=base_identity,
        base_profile=effective_profile,
    )
    created, existing, record_ids = _enqueue_plan(
        executable,
        plan,
        base_identity=base_identity,
        base_profile=effective_profile,
    )
    return plan, QueueWriteResult(
        created_rows=created,
        existing_rows=existing,
        record_ids=record_ids,
    )


def _limit_plan_items(plan: AlertQueuePlan, *, max_items: int | None) -> AlertQueuePlan:
    if max_items is None:
        return plan
    if type(max_items) is not int or max_items <= 0:
        raise AlertNotificationError("max_items must be a positive integer")
    items = plan.items[:max_items]
    selected_pairs = {(item.company_code, item.monitor_code) for item in items}
    return replace(
        plan,
        selections=tuple(
            selection
            for selection in plan.selections
            if (selection.company_code, selection.monitor_code) in selected_pairs
        ),
        items=items,
        skipped=tuple(
            skipped
            for skipped in plan.skipped
            if (skipped.company_code, skipped.monitor_code) in selected_pairs
        ),
    )


def _preview_payload(
    report: dict[str, object],
    plan: AlertQueuePlan,
    *,
    source_record_count: int,
    excluded_blank_company_count: int,
    max_per_monitor: int,
    max_items: int | None,
    base_identity: str,
    base_profile: str | None,
) -> dict[str, object]:
    return {
        "schema_version": 2,
        "delivery_mode": "manual_base_checkbox",
        "generated_at": datetime.now(UTC).isoformat(),
        "source_report_generated_at": report.get("generated_at"),
        "fiscal_year": report.get("fiscal_year"),
        "quarter": report.get("quarter"),
        "through_period": report.get("through_period"),
        "selection_rule": (
            "按六项能力固定顺序及报告公司顺序，每项取前"
            f"{max_per_monitor}家状态为ALERT的公司；每个公司与能力生成一条独立队列明细；"
            + (f"本次最多入队{max_items}条" if max_items is not None else "不限制最终入队条数")
        ),
        "manual_delivery_contract": {
            "record_creation_sends_message": False,
            "message_trigger": "用户在示警推送队列具体明细行勾选推送",
            "recipient_resolution": "点击时通过法人主体关联记录实时读取业财字段",
        },
        "base_mapping": {
            "base_token": BASE_TOKEN,
            "main_table_id": MAIN_TABLE_ID,
            "queue_table_id": QUEUE_TABLE_ID,
            "identity": base_identity,
            "profile": base_profile,
            "source_record_count": source_record_count,
            "excluded_blank_company_count": excluded_blank_company_count,
        },
        "counts": {
            "selected_monitor_company_pairs": len(plan.selections),
            "planned_queue_rows": len(plan.items),
            "skipped_rows": len(plan.skipped),
        },
        "queue_rows": [
            {
                "idempotency_key": item.idempotency_key,
                "company_code": item.company_code,
                "company_name": item.company_name,
                "monitor_code": item.monitor_code,
                "monitor_name": item.monitor_name,
                "period": item.period,
                "outcome": item.outcome,
                "key_values": item.key_values,
                "alert_details": item.alert_details,
                "dashboard_url": item.dashboard_url,
                "test_push": item.test_push,
            }
            for item in plan.items
        ],
        "skipped": [
            {
                "company_code": skipped.company_code,
                "company_name": skipped.company_name,
                "monitor_code": skipped.monitor_code,
                "reason_code": skipped.reason_code,
                "reason": skipped.reason,
            }
            for skipped in plan.skipped
        ],
    }


def _write_json_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--report",
        type=Path,
        default=REPO_ROOT / "web" / "public" / "real-validation-latest.json",
    )
    parser.add_argument(
        "--preview",
        type=Path,
        default=(
            REPO_ROOT / "artifacts" / "notifications" / "feishu-alert-queue-preview-latest.json"
        ),
    )
    parser.add_argument("--dashboard-url", default=DEFAULT_DASHBOARD_URL)
    parser.add_argument("--max-per-monitor", type=int, default=3, choices=(1, 2, 3))
    parser.add_argument("--max-items", type=int)
    parser.add_argument("--company-code", action="append", dest="company_codes")
    parser.add_argument("--base-as", choices=("user", "bot"), default="bot")
    parser.add_argument("--base-profile", default=DEFAULT_BASE_PROFILE)
    parser.add_argument("--test-push", action="store_true")
    parser.add_argument("--enqueue", "--execute", dest="enqueue", action="store_true")
    args = parser.parse_args()

    if args.max_items is not None and args.max_items <= 0:
        parser.error("--max-items must be a positive integer")
    base_profile = args.base_profile if args.base_as == "bot" else None
    cli = shutil.which("lark-cli")
    if cli is None:
        raise LarkCliError("lark-cli is not installed")

    report_path = args.report.resolve()
    report = _load_json_object(report_path)
    directory = _load_company_directory(
        cli,
        base_identity=args.base_as,
        base_profile=base_profile,
    )
    plan = build_queue_plan(
        report,
        directory,
        max_companies_per_monitor=args.max_per_monitor,
        dashboard_url=args.dashboard_url,
        test_push=args.test_push,
        company_codes=args.company_codes,
    )
    plan = _limit_plan_items(plan, max_items=args.max_items)
    preview = _preview_payload(
        report,
        plan,
        source_record_count=directory.source_record_count,
        excluded_blank_company_count=directory.excluded_blank_company_count,
        max_per_monitor=args.max_per_monitor,
        max_items=args.max_items,
        base_identity=args.base_as,
        base_profile=base_profile,
    )
    preview_path = args.preview.resolve()
    _write_json_atomic(preview_path, preview)

    created = 0
    already_queued = 0
    record_ids: tuple[str, ...] = ()
    if args.enqueue:
        _validate_queue_schema(
            cli,
            base_identity=args.base_as,
            base_profile=base_profile,
        )
        created, already_queued, record_ids = _enqueue_plan(
            cli,
            plan,
            base_identity=args.base_as,
            base_profile=base_profile,
        )

    print(f"preview: {preview_path}")
    print(
        f"planned queue rows: {len(plan.items)}; skipped rows: {len(plan.skipped)}; "
        "automatic messages sent: 0"
    )
    if args.enqueue:
        print(
            f"queue rows created: {created}; already queued: {already_queued}; "
            f"record ids: {','.join(record_ids)}"
        )
    return 0 if not plan.skipped else 2


if __name__ == "__main__":
    raise SystemExit(main())
