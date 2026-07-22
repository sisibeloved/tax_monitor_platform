"""Opt-in write verification against a dedicated, non-production Lark Base.

The test is skipped unless ``LARK_LIVE_TEST_ALLOW_WRITE=true``. A run also requires
``LARK_LIVE_TEST_DEDICATED_BASE=true`` plus explicit App credentials, resource IDs,
an isolated ``LIVE-TEST-`` company code, its expected initial status, and a different
desired status. The record is restored to its expected initial status before exit.
"""

from __future__ import annotations

import os

import pytest

from tax_risk.adapters.lark.refund_base import LarkRefundBaseClient, LarkRefundBaseConfig


_PRODUCTION_BASE_TOKEN = "A1Kwb4tkZaZdE2s3C2dcG49Fn2d"
_PRODUCTION_TABLE_ID = "tbl4PCNdcl4BYzgZ"

pytestmark = pytest.mark.skipif(
    os.getenv("LARK_LIVE_TEST_ALLOW_WRITE", "").lower() != "true",
    reason="set LARK_LIVE_TEST_ALLOW_WRITE=true for the dedicated Base write test",
)


def _required_environment(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        pytest.fail(f"{name} is required for the opt-in Lark Base live test")
    return value


def test_dedicated_lark_base_app_can_write_read_back_and_restore_status() -> None:
    if os.getenv("LARK_LIVE_TEST_DEDICATED_BASE", "").lower() != "true":
        pytest.fail("LARK_LIVE_TEST_DEDICATED_BASE=true is required")

    base_token = _required_environment("LARK_LIVE_TEST_BASE_TOKEN")
    table_id = _required_environment("LARK_LIVE_TEST_TABLE_ID")
    if base_token == _PRODUCTION_BASE_TOKEN or table_id == _PRODUCTION_TABLE_ID:
        pytest.fail("the live write test must not target the configured production Base")

    company_code = _required_environment("LARK_LIVE_TEST_COMPANY_CODE")
    if not company_code.startswith("LIVE-TEST-"):
        pytest.fail("LARK_LIVE_TEST_COMPANY_CODE must start with LIVE-TEST-")
    expected_initial_value = _required_environment("LARK_LIVE_TEST_EXPECTED_INITIAL_VALUE")
    desired_value = _required_environment("LARK_LIVE_TEST_DESIRED_VALUE")
    if desired_value == expected_initial_value:
        pytest.fail("the desired live-test value must differ from the expected initial value")

    config = LarkRefundBaseConfig(
        base_token=base_token,
        table_id=table_id,
        company_code_field_id=_required_environment(
            "LARK_LIVE_TEST_COMPANY_CODE_FIELD_ID"
        ),
        status_field_id=_required_environment("LARK_LIVE_TEST_STATUS_FIELD_ID"),
        app_id=_required_environment("LARK_LIVE_TEST_APP_ID"),
        app_secret=_required_environment("LARK_LIVE_TEST_APP_SECRET"),
        api_base_url="https://open.feishu.cn",
        timeout=30,
        page_size=10,
    )

    with LarkRefundBaseClient(config) as client:
        preflight = client.preflight(company_code)
        assert preflight.company_code_field_type == "text"
        assert preflight.status_field_type == "text"
        assert preflight.status_value == expected_initial_value
        initial_value = client.read_status(company_code)
        assert initial_value == expected_initial_value

        write_attempted = False
        try:
            write_attempted = True
            result = client.write_status(company_code, desired_value)
            assert result.updated is True
            assert result.previous_value == expected_initial_value
            assert client.read_status(company_code) == desired_value
        finally:
            if write_attempted:
                current_value = client.read_status(company_code)
                if current_value != expected_initial_value:
                    client.write_status(company_code, expected_initial_value)
                assert client.read_status(company_code) == expected_initial_value
