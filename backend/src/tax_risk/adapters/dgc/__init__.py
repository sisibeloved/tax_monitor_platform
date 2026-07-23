"""Huawei DGC data-service adapters."""

from tax_risk.adapters.dgc.hesi_business_entertainment import (
    HesiApplicationClient,
    HesiApplicationClientConfiguration,
    HesiBusinessDataClientError,
    HesiDetailClient,
    HesiDetailClientConfiguration,
    HesiInvoiceClient,
    HesiInvoiceClientConfiguration,
)

from tax_risk.adapters.dgc.sap_income import (
    SapIncomeClient,
    SapIncomeClientConfiguration,
    SapIncomeClientError,
)
from tax_risk.adapters.dgc.settlement_adjustment import (
    SettlementAdjustmentClient,
    SettlementAdjustmentClientConfiguration,
    SettlementAdjustmentClientError,
    build_apig_headers,
)
from tax_risk.adapters.dgc.trial_balance import (
    TrialBalanceClient,
    TrialBalanceClientConfiguration,
    TrialBalanceClientError,
)

__all__ = [
    "HesiApplicationClient",
    "HesiApplicationClientConfiguration",
    "HesiBusinessDataClientError",
    "HesiDetailClient",
    "HesiDetailClientConfiguration",
    "HesiInvoiceClient",
    "HesiInvoiceClientConfiguration",
    "SapIncomeClient",
    "SapIncomeClientConfiguration",
    "SapIncomeClientError",
    "SettlementAdjustmentClient",
    "SettlementAdjustmentClientConfiguration",
    "SettlementAdjustmentClientError",
    "TrialBalanceClient",
    "TrialBalanceClientConfiguration",
    "TrialBalanceClientError",
    "build_apig_headers",
]
