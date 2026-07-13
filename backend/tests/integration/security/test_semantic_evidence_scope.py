from __future__ import annotations

from datetime import date
from decimal import Decimal
from uuid import uuid4

import pytest

from tax_risk.application.semantic.evidence_reader import (
    EvidenceNotFound,
    EvidenceProjection,
    EvidenceReader,
)
from tax_risk.security.principal import COMPANY_FINANCE_ROLE, Principal


class _EvidenceRepository:
    def __init__(self, *records: EvidenceProjection) -> None:
        self.records = {record.reference_id: record for record in records}

    def read_by_reference(self, reference_id):
        return self.records.get(reference_id)


def _principal(company_id):
    return Principal(
        subject="company-user",
        roles=frozenset({COMPANY_FINANCE_ROLE}),
        allowed_company_ids=frozenset({company_id}),
        organization_path="/group/company",
    )


def test_evidence_reader_hides_other_company_references() -> None:
    own_company = uuid4()
    other_company = uuid4()
    own = EvidenceProjection(
        reference_id=uuid4(),
        company_id=own_company,
        company_code="C001",
        dataset_code="sap_welfare",
        period=date(2026, 6, 30),
        amount=Decimal("100.00"),
        currency="CNY",
        payload={"summary": "员工体检"},
    )
    other = EvidenceProjection(
        reference_id=uuid4(),
        company_id=other_company,
        company_code="C002",
        dataset_code="sap_welfare",
        period=date(2026, 6, 30),
        amount=Decimal("200.00"),
        currency="CNY",
        payload={"summary": "客户礼品"},
    )
    reader = EvidenceReader(_EvidenceRepository(own, other))

    assert reader.read_by_reference(_principal(own_company), own.reference_id) == own
    with pytest.raises(EvidenceNotFound):
        reader.read_by_reference(_principal(own_company), other.reference_id)

