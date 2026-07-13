from __future__ import annotations

import pytest

from tax_risk.domain.semantic.contracts import SemanticModelJudgment
from tax_risk.model_gateway.policy import ModelGatewayPolicy, ProviderPolicy
from tax_risk.model_gateway.service import ProtectedModelGateway, TechnicalReviewRequired


class _Client:
    def __init__(self, response: object) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    async def generate(self, *, system_prompt, input_json, output_model):
        self.calls.append(input_json)
        return self.response


@pytest.mark.anyio
async def test_gateway_revalidates_strict_judgment_and_retries_schema_once() -> None:
    client = _Client(
        {
            "semantic_label": "MEETING_EXPENSE",
            "confidence_tier": "HIGH",
            "evidence_citations": [],
            "recommended_account_ids": ["MEETING"],
            "rationale_summary": "根据会议材料判断。",
            "missing_evidence": [],
            "company_code": "FORGED",
        }
    )
    gateway = ProtectedModelGateway(
        client,
        ModelGatewayPolicy(
            ProviderPolicy(
                environment="test",
                no_public_training=True,
                retention_mode="zero",
            )
        ),
    )

    with pytest.raises(TechnicalReviewRequired) as raised:
        await gateway.generate(
            system_prompt="仅返回结构化判断",
            input_json={"evidence": []},
            output_model=SemanticModelJudgment,
        )

    assert raised.value.code == "MODEL_SCHEMA_INVALID"
    assert len(client.calls) == 2

