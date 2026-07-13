from __future__ import annotations

from decimal import Decimal

from tax_risk.application.semantic.sap_voucher_agent import SapVoucherPolicy
from tax_risk.domain.cases import MonitorType
from tax_risk.domain.semantic.contracts import SemanticLabel
from tax_risk.domain.semantic.sap_voucher import AccountFamily


DONATION_POLICY = SapVoucherPolicy(
    monitoring_type=MonitorType.DONATION,
    account_family=AccountFamily.DONATION,
    limit_rate=Decimal("0.12"),
    allowed_labels=frozenset(
        {
            SemanticLabel.CURRENT_ACCOUNT_REASONABLE,
            SemanticLabel.SPONSORSHIP,
            SemanticLabel.ADVERTISING_PROMOTION,
            SemanticLabel.INSUFFICIENT_EVIDENCE,
        }
    ),
    suspicious_labels=frozenset(
        {SemanticLabel.SPONSORSHIP, SemanticLabel.ADVERTISING_PROMOTION}
    ),
    system_prompt=(
        "你是公益性捐赠入账复核Agent。只根据给定SAP凭证证据判断，不补造事实。"
        "赞助倾向SPONSORSHIP；冠名、广告权益、品牌露出等对价倾向"
        "ADVERTISING_PROMOTION或SPONSORSHIP。材料不足返回INSUFFICIENT_EVIDENCE，"
        "现有科目合理返回CURRENT_ACCOUNT_REASONABLE，不作最终税务定性。"
    ),
)


__all__ = ["DONATION_POLICY"]
