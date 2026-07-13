from __future__ import annotations

from tax_risk.application.exports import export_authorization_version


def test_authorization_version_is_deterministic_without_raw_scope_values() -> None:
    first = export_authorization_version(
        subject="user",
        roles=frozenset({"company-finance"}),
        company_ids=frozenset(),
    )
    second = export_authorization_version(
        subject="user",
        roles=frozenset({"company-finance"}),
        company_ids=frozenset(),
    )
    assert first == second
    assert len(first) == 64
    assert "user" not in first

