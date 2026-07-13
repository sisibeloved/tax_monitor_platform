from __future__ import annotations

from tax_risk.application.audit import normalized_filter_hash, redact_summary


def test_sensitive_text_is_replaced_by_stable_digest() -> None:
    payload = {
        "status": "CLOSED",
        "reason": "包含员工姓名和说明",
        "phone": "13800138000",
        "attachment": "object://private/evidence.pdf",
    }

    first = redact_summary(payload)
    second = redact_summary(payload)

    assert first == second
    assert first["status"] == "CLOSED"
    assert first["reason"] == {"sha256": first["reason"]["sha256"]}
    assert len(first["reason"]["sha256"]) == 64
    assert "13800138000" not in repr(first)
    assert "evidence.pdf" not in repr(first)


def test_filter_hash_is_order_independent_and_contains_no_values() -> None:
    left = normalized_filter_hash({"company": "C001", "status": ["NEW", "CLOSED"]})
    right = normalized_filter_hash({"status": ["NEW", "CLOSED"], "company": "C001"})

    assert left == right
    assert len(left) == 64
    assert "C001" not in left

