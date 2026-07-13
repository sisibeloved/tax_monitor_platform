from tax_risk.release.scorecard import (
    AcceptanceMetrics,
    EvidenceReference,
    ProductionScorecard,
    _evidence_reference,
)


def _metrics(**overrides: object) -> AcceptanceMetrics:
    values: dict[str, object] = {
        "formula_accuracy": 1.0,
        "traceability_rate": 1.0,
        "master_data_block_rate": 1.0,
        "valid_company_success_rate": 0.99,
        "semantic_recall": 0.96,
        "high_confidence_accuracy": 0.82,
        "known_semantic_misses": 0,
        "maximum_delivery_hours": 48.0,
        "authorization_isolation_passed": True,
        "external_semantic_index_configured": False,
        "audit_immutability_passed": True,
        "signature_verified": True,
        "recovery_verified": True,
        "rollback_verified": True,
    }
    values.update(overrides)
    return AcceptanceMetrics(**values)


def _evidence() -> dict[str, EvidenceReference]:
    return {
        gate: EvidenceReference(
            reference=f"artifacts/acceptance/phase-4/{gate}.json",
            sha256=(index + 1).__format__("x").rjust(64, "0"),
            verified=True,
        )
        for index, gate in enumerate(ProductionScorecard.required_evidence_keys())
    }


def test_production_scorecard_requires_every_threshold_evidence_and_approval() -> None:
    result = ProductionScorecard().evaluate(
        metrics=_metrics(),
        evidence=_evidence(),
        approvals={
            "tax_owner": "tax-owner@example.com",
            "data_owner": "data-owner@example.com",
            "security_owner": "security-owner@example.com",
            "operations_owner": "operations-owner@example.com",
        },
        snapshot_set_id="pilot-2026q2",
    )

    assert result.production_ready is True
    assert result.failed_gates == ()


def test_missing_evidence_or_failed_metric_never_reports_production_ready() -> None:
    evidence = _evidence()
    evidence.pop("signature_verification")
    result = ProductionScorecard().evaluate(
        metrics=_metrics(semantic_recall=0.949),
        evidence=evidence,
        approvals={"tax_owner": "tax-owner@example.com"},
        snapshot_set_id="pilot-2026q2",
    )

    assert result.production_ready is False
    assert set(result.failed_gates) >= {
        "PRODUCTION_RECALL_BELOW_95_PERCENT",
        "MISSING_EVIDENCE_SIGNATURE_VERIFICATION",
        "MISSING_APPROVAL_DATA_OWNER",
        "MISSING_APPROVAL_SECURITY_OWNER",
        "MISSING_APPROVAL_OPERATIONS_OWNER",
    }


def test_generated_evidence_reference_is_repository_portable(tmp_path) -> None:
    evidence_path = tmp_path / "artifacts" / "acceptance" / "phase-4" / "security.json"
    evidence_path.parent.mkdir(parents=True)
    evidence_path.write_text("{}\n", encoding="utf-8")

    reference = _evidence_reference(evidence_path)

    assert reference.reference == "artifacts/acceptance/phase-4/security.json"
