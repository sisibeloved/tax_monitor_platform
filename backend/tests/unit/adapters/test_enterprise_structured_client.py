from __future__ import annotations

import asyncio

import httpx
import pytest

from tax_risk.adapters.model.enterprise_structured_client import (
    EnterpriseModelConfiguration,
    EnterpriseStructuredModelClient,
)
from tax_risk.domain.semantic.contracts import SemanticModelJudgment


def _response_payload() -> dict[str, object]:
    return {
        "semantic_label": "MEETING_EXPENSE",
        "confidence_tier": "HIGH",
        "evidence_citations": [
            {
                "evidence_id": "evidence-1",
                "field_name": "summary",
                "quoted_text": "内部会议餐",
            }
        ],
        "recommended_account_ids": ["MEETING_EXPENSE"],
        "rationale_summary": "现有资料显示可能应计入会议费。",
        "missing_evidence": [],
    }


def test_enterprise_client_sends_schema_only_to_https_zero_retention_endpoint() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        captured["json"] = __import__("json").loads(request.content)
        return httpx.Response(
            200,
            json={
                "output": _response_payload(),
                "usage": {"total_tokens": 42},
                "zero_retention_confirmed": True,
            },
        )

    transport = httpx.MockTransport(handler)
    client = EnterpriseStructuredModelClient(
        EnterpriseModelConfiguration(
            endpoint="https://model.internal.example/v1/generate",
            deployment="income-tax-v1",
            timeout_seconds=10,
            credential_ref="secret://tax-risk/model",
            zero_retention_required=True,
        ),
        credential_resolver=lambda ref: "controlled-token" if ref.startswith("secret://") else "",
        transport=transport,
    )

    result = asyncio.run(
        client.generate(
            system_prompt="只返回结构化专业判断。",
            input_json={"summary": "内部会议餐"},
            output_model=SemanticModelJudgment,
        )
    )

    assert result.semantic_label.value == "MEETING_EXPENSE"
    request_json = captured["json"]
    assert isinstance(request_json, dict)
    assert request_json["deployment"] == "income-tax-v1"
    assert "tools" not in request_json
    assert request_json["response_schema"] == SemanticModelJudgment.model_json_schema()
    headers = captured["headers"]
    assert isinstance(headers, dict)
    assert headers["x-zero-retention-required"] == "true"


@pytest.mark.parametrize(
    "overrides",
    [
        {"endpoint": "http://model.internal.example/v1/generate"},
        {"zero_retention_required": False},
        {"credential_ref": ""},
    ],
)
def test_enterprise_configuration_fails_closed(overrides: dict[str, object]) -> None:
    values: dict[str, object] = {
        "endpoint": "https://model.internal.example/v1/generate",
        "deployment": "income-tax-v1",
        "timeout_seconds": 10,
        "credential_ref": "secret://tax-risk/model",
        "zero_retention_required": True,
    }
    values.update(overrides)
    with pytest.raises(ValueError):
        EnterpriseModelConfiguration.model_validate(values)
