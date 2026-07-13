from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError

from tax_risk.domain.cases import MonitorType
from tax_risk.domain.semantic.account_dictionary import (
    AccountEntryStatus,
    SuggestedAccountEntry,
)
from tax_risk.domain.semantic.contracts import (
    ConfidenceTier,
    SemanticDetection,
    SemanticLabel,
    SemanticVersionSet,
)


def test_welfare_and_donation_labels_are_controlled_enum_members() -> None:
    assert SemanticLabel.BUSINESS_ENTERTAINMENT.value == "BUSINESS_ENTERTAINMENT"
    assert SemanticLabel.ADVERTISING_PROMOTION.value == "ADVERTISING_PROMOTION"
    assert SemanticLabel.SPONSORSHIP.value == "SPONSORSHIP"


def test_account_entry_rejects_unknown_monitor_or_label() -> None:
    valid = {
        "account_id": "BUSINESS_ENTERTAINMENT_EXPENSE",
        "account_code": "660101",
        "account_name": "业务招待费",
        "accounting_classification": "PERIOD_EXPENSE",
        "allowed_monitor_types": ["WELFARE"],
        "allowed_labels": ["BUSINESS_ENTERTAINMENT"],
        "status": "ACTIVE",
    }

    entry = SuggestedAccountEntry.model_validate(valid)
    assert entry.allowed_monitor_types == (MonitorType.WELFARE,)
    assert entry.allowed_labels == (SemanticLabel.BUSINESS_ENTERTAINMENT,)

    for field, unknown in (
        ("allowed_monitor_types", "UNKNOWN_MONITOR"),
        ("allowed_labels", "UNKNOWN_LABEL"),
    ):
        invalid = valid | {field: [unknown]}
        with pytest.raises(ValidationError, match=field):
            SuggestedAccountEntry.model_validate(invalid)


def test_phase_3_detection_freezes_monitor_and_dictionary_version() -> None:
    detection = SemanticDetection(
        detection_key="welfare|1001|2026|510001|001|model-v2",
        candidate_key="welfare|1001|2026|510001|001",
        company_code="1001",
        fiscal_year=2026,
        period=6,
        monitoring_type=MonitorType.WELFARE,
        source_mode="SAP_LINKED",
        canonical_source_record_id=uuid4(),
        sap_observation_id=uuid4(),
        sap_document_number="510001",
        sap_line_item="001",
        amount=Decimal("100.00"),
        currency="CNY",
        snapshot_id=uuid4(),
        exact_evidence_link_id=None,
        versions=SemanticVersionSet(
            rule_version_id="rule-v2",
            model_version_id="model-v2",
            prompt_version_id="prompt-v2",
            case_library_version_id="cases-v2",
            account_dictionary_version="candidate-accounts-v2",
        ),
        semantic_label=SemanticLabel.BUSINESS_ENTERTAINMENT,
        confidence_tier=ConfidenceTier.HIGH,
        evidence_refs=[],
        recommended_account_ids=["BUSINESS_ENTERTAINMENT_EXPENSE"],
        rationale_summary="客户商务宴请更符合业务招待费。",
        missing_evidence=[],
        detected_at=datetime.now(timezone.utc),
    )

    assert detection.monitoring_type is MonitorType.WELFARE
    assert detection.versions.account_dictionary_version == "candidate-accounts-v2"


def test_account_entry_is_immutable() -> None:
    entry = SuggestedAccountEntry(
        account_id="SPONSORSHIP_EXPENSE",
        account_code="660301",
        account_name="赞助支出",
        accounting_classification="PERIOD_EXPENSE",
        allowed_monitor_types=(MonitorType.DONATION,),
        allowed_labels=(SemanticLabel.SPONSORSHIP,),
        status=AccountEntryStatus.ACTIVE,
    )

    with pytest.raises(ValidationError, match="frozen"):
        entry.account_name = "广告宣传费"
