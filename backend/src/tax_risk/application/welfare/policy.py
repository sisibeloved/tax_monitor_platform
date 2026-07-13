from __future__ import annotations

from decimal import Decimal

from tax_risk.application.semantic.sap_voucher_agent import SapVoucherPolicy
from tax_risk.domain.cases import MonitorType
from tax_risk.domain.semantic.contracts import SemanticLabel
from tax_risk.domain.semantic.sap_voucher import AccountFamily


WELFARE_POLICY = SapVoucherPolicy(
    monitoring_type=MonitorType.WELFARE,
    account_family=AccountFamily.WELFARE,
    limit_rate=Decimal("0.14"),
    allowed_labels=frozenset(
        {
            SemanticLabel.CURRENT_ACCOUNT_REASONABLE,
            SemanticLabel.BUSINESS_ENTERTAINMENT,
            SemanticLabel.EMPLOYEE_EDUCATION,
            SemanticLabel.ADVERTISING_PROMOTION,
            SemanticLabel.INSUFFICIENT_EVIDENCE,
        }
    ),
    suspicious_labels=frozenset(
        {
            SemanticLabel.BUSINESS_ENTERTAINMENT,
            SemanticLabel.EMPLOYEE_EDUCATION,
            SemanticLabel.ADVERTISING_PROMOTION,
        }
    ),
    system_prompt=(
        "你是福利费入账复核Agent。只根据给定SAP凭证证据判断，不补造事实。"
        "客户、供应商、政府接待或商务宴请倾向BUSINESS_ENTERTAINMENT；"
        "培训费、讲师费、考试费倾向EMPLOYEE_EDUCATION；"
        "宣传赠品倾向ADVERTISING_PROMOTION；客户礼品可在广告宣传与业务招待间判断。"
        "材料不足返回INSUFFICIENT_EVIDENCE，入账合理返回CURRENT_ACCOUNT_REASONABLE。"
    ),
)


__all__ = ["WELFARE_POLICY"]
