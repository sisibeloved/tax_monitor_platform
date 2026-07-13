"""Single-item professional agent using only authorized evidence fields."""

from __future__ import annotations

from datetime import date

from tax_risk.application.business_entertainment.evidence_review import (
    BusinessEntertainmentEvidencePack,
)
from tax_risk.application.semantic.model_client import StructuredModelClient
from tax_risk.application.semantic.prompt_safety import minimize_model_input
from tax_risk.domain.semantic.contracts import (
    SemanticModelJudgment,
    SemanticVersionSet,
)


_SYSTEM_PROMPT = (
    "你是集团所得税业务招待费入账复核助手。仅依据获准证据返回JSON结构化判断；"
    "不得推断公司、金额、SAP标识或版本，不得输出分析过程，使用审慎的不确定性措辞。"
)


class BusinessEntertainmentProfessionalAgent:
    def __init__(self, client: StructuredModelClient) -> None:
        self._client = client

    async def evaluate(
        self,
        *,
        evidence_pack: BusinessEntertainmentEvidencePack,
        current_account_name: str | None,
        document_date: date,
        versions: SemanticVersionSet,
    ) -> SemanticModelJudgment:
        evidence: list[dict[str, object]] = [
            {
                "evidence_id": field.evidence_id,
                "field_name": field.field_name,
                "value": _safe_evidence_text(field.value),
            }
            for field in evidence_pack.fields
        ]
        return await self._client.generate(
            system_prompt=_SYSTEM_PROMPT,
            input_json={
                "current_account_name": current_account_name or "待定位SAP凭证",
                "document_date": document_date.isoformat(),
                "evidence": evidence,
                "account_dictionary_version": versions.account_dictionary_version,
            },
            output_model=SemanticModelJudgment,
        )


def _safe_evidence_text(value: str) -> str:
    minimized = minimize_model_input(
        {"evidence_text": value},
        allowed_fields=frozenset({"evidence_text"}),
    )
    safe_value = minimized.get("evidence_text")
    if not isinstance(safe_value, str):
        raise ValueError("evidence text could not be minimized")
    return safe_value


__all__ = ["BusinessEntertainmentProfessionalAgent"]
