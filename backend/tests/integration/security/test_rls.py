from __future__ import annotations

from sqlalchemy import text


def test_company_scoped_tables_have_forced_rls(engine) -> None:
    expected = {
        "accounting_snapshot",
        "detection_record",
        "risk_case",
        "source_record",
        "tax_master_version",
        "semantic_detection_record",
        "semantic_evidence_task",
        "evidence_link",
        "sap_link_coverage",
    }
    with engine.connect() as connection:
        rows = connection.execute(
            text(
                "SELECT c.relname, c.relrowsecurity, c.relforcerowsecurity "
                "FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = current_schema() AND c.relname = ANY(:tables)"
            ),
            {"tables": sorted(expected)},
        ).all()

    assert {name for name, enabled, forced in rows if enabled and forced} == expected

