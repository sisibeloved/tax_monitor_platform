from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError


def test_audit_event_cannot_be_updated_or_deleted(engine) -> None:
    event_id = uuid4()
    entity_id = uuid4()
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO audit_event (
                    id, entity_type, entity_id, action, actor, payload,
                    actor_roles, company_ids, result
                ) VALUES (
                    :id, 'RISK_CASE', :entity_id, 'READ', 'auditor', '{}'::jsonb,
                    '["audit"]'::jsonb, '[]'::jsonb, 'SUCCEEDED'
                )
                """
            ),
            {"id": event_id, "entity_id": entity_id},
        )

    with pytest.raises(DBAPIError):
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE audit_event SET action = 'TAMPERED' WHERE id = :id"),
                {"id": event_id},
            )
    with pytest.raises(DBAPIError):
        with engine.begin() as connection:
            connection.execute(
                text("DELETE FROM audit_event WHERE id = :id"),
                {"id": event_id},
            )

    with engine.connect() as connection:
        assert connection.execute(
            text("SELECT action FROM audit_event WHERE id = :id"), {"id": event_id}
        ).scalar_one() == "READ"

