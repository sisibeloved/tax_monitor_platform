from __future__ import annotations

import asyncio
import json

import httpx

from tax_risk.adapters.model.enterprise_structured_client import (
    EnterpriseModelConfiguration,
    EnterpriseStructuredModelClient,
    InMemoryModelCallAuditSink,
    ModelCallContext,
)
from tax_risk.application.semantic.prompt_safety import minimize_model_input
from tax_risk.domain.semantic.contracts import SemanticModelJudgment


def test_pii_is_removed_and_audit_contains_checksums_not_raw_text(caplog: object) -> None:
    raw = {
        "summary": "内部会议餐，联系人张三，电话13800138000，证件11010519491231002X",
        "participant_name": "张三",
        "phone": "13800138000",
        "id_card": "11010519491231002X",
        "unapproved_field": "不得发送",
    }
    minimized = minimize_model_input(raw, allowed_fields=frozenset({"summary"}))
    serialized = json.dumps(minimized, ensure_ascii=False)
    for sensitive in ("张三", "13800138000", "11010519491231002X", "不得发送"):
        assert sensitive not in serialized

    captured_body: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured_body.update(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "output": {
                    "semantic_label": "INSUFFICIENT_EVIDENCE",
                    "confidence_tier": "LOW",
                    "evidence_citations": [],
                    "recommended_account_ids": ["MANUAL_REVIEW"],
                    "rationale_summary": "现有证据不足，建议人工复核。",
                    "missing_evidence": ["接待对象类别"],
                },
                "usage": {"total_tokens": 12},
                "zero_retention_confirmed": True,
            },
        )

    sink = InMemoryModelCallAuditSink()
    client = EnterpriseStructuredModelClient(
        EnterpriseModelConfiguration(
            endpoint="https://model.internal.example/generate",
            deployment="income-tax-v1",
            timeout_seconds=5,
            credential_ref="secret://model",
            zero_retention_required=True,
        ),
        credential_resolver=lambda _: "token",
        transport=httpx.MockTransport(handler),
        audit_sink=sink,
        context=ModelCallContext(
            candidate_key="candidate-1",
            company_code="C001",
            model_version_id="model-v1",
            prompt_version_id="prompt-v1",
            case_library_version_id="cases-v1",
            operator_id="worker",
            run_id="run-1",
        ),
    )
    asyncio.run(
        client.generate(
            system_prompt="只返回结构化判断。",
            input_json=minimized,
            output_model=SemanticModelJudgment,
        )
    )

    request_text = json.dumps(captured_body, ensure_ascii=False)
    assert "tools" not in captured_body
    assert sink.records[0].allowed_fields == ("summary",)
    audit_text = sink.records[0].model_dump_json()
    logs = getattr(caplog, "text", "")
    for sensitive in ("内部会议餐", "张三", "13800138000", "11010519491231002X"):
        assert sensitive not in audit_text
        assert sensitive not in logs
    for sensitive in ("张三", "13800138000", "11010519491231002X"):
        assert sensitive not in request_text
    assert "内部会议餐" in request_text
    assert len(sink.records[0].request_checksum) == 64
    assert len(sink.records[0].output_checksum) == 64


def test_missing_zero_retention_confirmation_blocks_result_and_audits_failure() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"output": {}, "zero_retention_confirmed": False})

    sink = InMemoryModelCallAuditSink()
    client = EnterpriseStructuredModelClient(
        EnterpriseModelConfiguration(
            endpoint="https://model.internal.example/generate",
            deployment="income-tax-v1",
            timeout_seconds=5,
            credential_ref="secret://model",
            zero_retention_required=True,
        ),
        credential_resolver=lambda _: "token",
        transport=httpx.MockTransport(handler),
        audit_sink=sink,
    )
    try:
        asyncio.run(
            client.generate(
                system_prompt="prompt",
                input_json={"summary": "safe"},
                output_model=SemanticModelJudgment,
            )
        )
    except RuntimeError as error:
        assert "retention" in str(error)
    else:
        raise AssertionError("missing zero-retention confirmation must fail closed")
    assert sink.records[-1].retention_confirmed is False
    assert sink.records[-1].schema_status == "BLOCKED"
