from __future__ import annotations

import json
import subprocess

import pytest

from scripts import enqueue_feishu_alert_notifications as notifications
from tax_risk.application.alert_notifications import AlertQueueItem, AlertQueuePlan


def _item(*, key: str = "taxrisk-queue-key", test_push: bool = True) -> AlertQueueItem:
    return AlertQueueItem(
        idempotency_key=key,
        company_record_id="rec_company",
        company_code="3000",
        company_name="测试公司",
        monitor_code="current_tax_accrual",
        monitor_name="季度应计提所得税准确性检查",
        period="2026年第2季度（截至6月）",
        outcome="计提金额不一致",
        key_values="本季度应计提所得税 100.00元",
        alert_details="差异 10.00元",
        dashboard_url="https://example.test/dashboard",
        test_push=test_push,
    )


def test_run_lark_cli_rejects_an_identity_mismatch(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["lark-cli"],
            returncode=0,
            stdout=json.dumps({"ok": True, "identity": "user", "data": {}}),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(notifications.LarkCliError, match="unexpected identity"):
        notifications._run_lark_cli(
            "lark-cli",
            ["base", "+record-list"],
            expected_identity="bot",
        )


def test_run_lark_cli_places_profile_before_the_domain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[str] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        observed.extend(args)
        return subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout=json.dumps({"ok": True, "identity": "bot", "data": {}}),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    notifications._run_lark_cli(
        "lark-cli",
        ["base", "+record-list"],
        expected_identity="bot",
        profile="tax-risk-notifier",
    )

    assert observed[:4] == [
        "lark-cli",
        "--profile",
        "tax-risk-notifier",
        "base",
    ]


def test_run_lark_cli_preserves_safe_api_error_details(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=["lark-cli"],
            returncode=1,
            stdout=json.dumps(
                {
                    "ok": False,
                    "error": {
                        "type": "api",
                        "subtype": "missing_scope",
                        "code": 99991672,
                        "message": "permission denied",
                        "missing_scopes": ["base:record:create"],
                        "console_url": "https://open.feishu.cn/app/example",
                    },
                }
            ),
            stderr="",
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    with pytest.raises(notifications.LarkCliError) as error:
        notifications._run_lark_cli("lark-cli", ["base", "+record-upsert"])

    message = str(error.value)
    assert "code=99991672" in message
    assert "base:record:create" in message
    assert "https://open.feishu.cn/app/example" in message


def test_queue_record_fields_link_company_and_never_store_recipient() -> None:
    fields = notifications._queue_record_fields(
        _item(),
        generated_at="2026-07-24 14:30:00",
    )

    assert fields[notifications.QUEUE_COMPANY_LINK_FIELD_ID] == [{"id": "rec_company"}]
    assert fields[notifications.QUEUE_STATUS_FIELD_ID] == "待推送"
    assert fields[notifications.QUEUE_PUSH_FIELD_ID] is False
    assert fields[notifications.QUEUE_TEST_PUSH_FIELD_ID] is True
    serialized = json.dumps(fields, ensure_ascii=False)
    assert "open_id" not in serialized
    assert "recipient" not in serialized


def test_enqueue_skips_existing_keys_and_creates_only_missing_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = _item(key="existing")
    second = _item(key="new")
    plan = AlertQueuePlan(selections=(), items=(first, second), skipped=())
    created: list[str] = []

    monkeypatch.setattr(
        notifications,
        "_load_existing_queue_keys",
        lambda *args, **kwargs: {"existing": ("rec_existing", "待推送")},
    )

    def create(*args: object, **kwargs: object) -> str:
        item = args[1]
        assert isinstance(item, AlertQueueItem)
        created.append(item.idempotency_key)
        return "rec_new"

    monkeypatch.setattr(notifications, "_create_queue_record", create)

    created_count, already_queued, record_ids = notifications._enqueue_plan(
        "lark-cli",
        plan,
        base_identity="bot",
        base_profile="tax-risk-notifier",
    )

    assert (created_count, already_queued) == (1, 1)
    assert created == ["new"]
    assert record_ids == ("rec_existing", "rec_new")


def test_create_queue_record_uses_only_base_record_api(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[str] = []

    def run(
        cli: str,
        arguments: list[str],
        **kwargs: object,
    ) -> dict[str, object]:
        observed.extend(arguments)
        return {
            "ok": True,
            "identity": "bot",
            "data": {"created": True, "record": {"record_id": "rec_created"}},
        }

    monkeypatch.setattr(notifications, "_run_lark_cli", run)

    record_id = notifications._create_queue_record(
        "lark-cli",
        _item(),
        generated_at="2026-07-24 14:30:00",
        base_identity="bot",
        base_profile="tax-risk-notifier",
    )

    assert record_id == "rec_created"
    assert observed[:2] == ["base", "+record-upsert"]
    assert "/open-apis/im/" not in " ".join(observed)
    assert "messages" not in observed


def test_create_queue_record_confirms_id_by_unique_key_when_response_omits_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        notifications,
        "_run_lark_cli",
        lambda *args, **kwargs: {
            "ok": True,
            "identity": "user",
            "data": {"created": True, "record": {"fields": {}}},
        },
    )
    monkeypatch.setattr(
        notifications,
        "_load_existing_queue_keys",
        lambda *args, **kwargs: {"taxrisk-queue-key": ("rec_confirmed", "待推送")},
    )

    record_id = notifications._create_queue_record(
        "lark-cli",
        _item(),
        generated_at="2026-07-24 14:30:00",
        base_identity="user",
        base_profile=None,
    )

    assert record_id == "rec_confirmed"
