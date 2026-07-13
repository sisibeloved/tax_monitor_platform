from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from tax_risk.application.semantic.evidence_reader import EvidenceProjection
from tax_risk.domain.semantic.contracts import SemanticModelJudgment
from tax_risk.model_gateway.policy import ModelGatewayPolicy, ProviderPolicy
from tax_risk.model_gateway.service import ProtectedModelGateway
from tax_risk.security.principal import COMPANY_FINANCE_ROLE, Principal


class _Reader:
    def __init__(self, allowed_company_id) -> None:
        self.allowed_company_id = allowed_company_id

    def read_by_reference(self, principal, reference_id):
        if self.allowed_company_id not in principal.allowed_company_ids:
            raise LookupError(reference_id)
        return EvidenceProjection(
            reference_id=reference_id,
            company_id=self.allowed_company_id,
            company_code="C001",
            dataset_code="sap_welfare",
            period=date(2026, 6, 30),
            amount=Decimal("100"),
            currency="CNY",
            payload={"summary": "员工培训餐"},
        )


class _Client:
    async def generate(self, *, system_prompt, input_json, output_model):
        assert set(input_json) == {"evidence"}
        return output_model.model_validate(
            {
                "semantic_label": "EMPLOYEE_EDUCATION",
                "confidence_tier": "HIGH",
                "evidence_citations": [],
                "recommended_account_ids": ["EDUCATION"],
                "rationale_summary": "证据显示为培训餐。",
                "missing_evidence": [],
            }
        )


@pytest.mark.anyio
async def test_gateway_reads_only_references_authorized_for_current_company() -> None:
    company_id = uuid4()
    gateway = ProtectedModelGateway(
        _Client(),
        ModelGatewayPolicy(
            ProviderPolicy(
                environment="test",
                no_public_training=True,
                retention_mode="zero",
            )
        ),
        evidence_reader=_Reader(company_id),
    )
    principal = Principal(
        subject="finance",
        roles=frozenset({COMPANY_FINANCE_ROLE}),
        allowed_company_ids=frozenset({company_id}),
        organization_path="/companies/c001",
    )

    result = await gateway.generate_from_references(
        principal=principal,
        reference_ids=(uuid4(),),
        system_prompt="仅根据证据判断",
        output_model=SemanticModelJudgment,
    )
    assert result.semantic_label.value == "EMPLOYEE_EDUCATION"

