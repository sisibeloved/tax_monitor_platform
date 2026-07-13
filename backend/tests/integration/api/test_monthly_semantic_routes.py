from __future__ import annotations

from functools import partial
from uuid import UUID, uuid4

from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

from tax_risk.config import Settings
from tax_risk.main import create_app
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory
from tax_risk.security.principal import COMPANY_FINANCE_ROLE, GROUP_TAX_ROLE, Principal
from tests.integration.persistence.test_monthly_semantic_repository import (
    _seed_monthly_set,
)


def test_trigger_and_get_welfare_run_freezes_all_versions(
    isolated_database_url: str,
) -> None:
    engine, factory = create_session_factory(isolated_database_url)
    dispatched: list[tuple[UUID, tuple[UUID, ...]]] = []
    try:
        snapshot_set_id, _, company_code = _seed_monthly_set(engine)
        semantic_version_set_id = _seed_semantic_versions(engine)
        principal = Principal(
            subject="group-tax@example.com",
            roles=frozenset({GROUP_TAX_ROLE}),
            allowed_company_ids=frozenset(),
            organization_path="/group/tax",
        )
        app = create_app(
            uow_factory=partial(UnitOfWork, factory),
            settings=Settings(environment="test"),
            principal_provider=lambda _request: principal,
            monthly_semantic_dispatcher=lambda **kwargs: dispatched.append(
                (kwargs["run_id"], kwargs["run_company_ids"])
            ),
        )
        client = TestClient(app)

        response = client.post(
            "/api/v1/monthly-semantic/runs",
            json={
                "monitoring_type": "WELFARE",
                "period": "2026-06",
                "company_codes": [company_code],
                "snapshot_set_id": str(snapshot_set_id),
                "semantic_version_set_id": str(semantic_version_set_id),
            },
        )

        assert response.status_code == 202, response.text
        body = response.json()
        assert body["monitoring_type"] == "WELFARE"
        assert body["snapshot_set_id"] == str(snapshot_set_id)
        assert body["semantic_version_set_id"] == str(semantic_version_set_id)
        assert body["frozen_versions"] == {
            "rule_version": "monthly-rule-v1",
            "model_version": "monthly-model-v1",
            "prompt_version": "monthly-prompt-v1",
            "case_library_version": "monthly-cases-v1",
            "account_dictionary_version": "candidate-accounts-v2",
        }
        assert len(dispatched) == 1 and len(dispatched[0][1]) == 1

        status = client.get(f"/api/v1/monthly-semantic/runs/{body['run_id']}")
        assert status.status_code == 200
        assert status.json()["companies"][0]["company_code"] == company_code
    finally:
        engine.dispose()


def test_company_finance_cannot_start_or_read_out_of_scope_run(
    isolated_database_url: str,
) -> None:
    engine, factory = create_session_factory(isolated_database_url)
    try:
        snapshot_set_id, _, company_code = _seed_monthly_set(engine)
        semantic_version_set_id = _seed_semantic_versions(engine)
        group = Principal(
            subject="group@example.com",
            roles=frozenset({GROUP_TAX_ROLE}),
            allowed_company_ids=frozenset(),
            organization_path="/group",
        )
        group_app = create_app(
            uow_factory=partial(UnitOfWork, factory),
            settings=Settings(environment="test"),
            principal_provider=lambda _request: group,
            monthly_semantic_dispatcher=lambda **_kwargs: None,
        )
        payload = {
            "monitoring_type": "DONATION",
            "period": "2026-06",
            "company_codes": [company_code],
            "snapshot_set_id": str(snapshot_set_id),
            "semantic_version_set_id": str(semantic_version_set_id),
        }
        created = TestClient(group_app).post("/api/v1/monthly-semantic/runs", json=payload)
        assert created.status_code == 202

        finance = Principal(
            subject="finance@example.com",
            roles=frozenset({COMPANY_FINANCE_ROLE}),
            allowed_company_ids=frozenset({uuid4()}),
            organization_path="/company/other",
        )
        finance_app = create_app(
            uow_factory=partial(UnitOfWork, factory),
            settings=Settings(environment="test"),
            principal_provider=lambda _request: finance,
            monthly_semantic_dispatcher=lambda **_kwargs: None,
        )
        assert TestClient(finance_app).post(
            "/api/v1/monthly-semantic/runs", json=payload
        ).status_code == 404
        assert TestClient(finance_app).get(
            f"/api/v1/monthly-semantic/runs/{created.json()['run_id']}"
        ).status_code == 404
    finally:
        engine.dispose()


def test_broker_failure_is_persisted_on_the_same_run(
    isolated_database_url: str,
) -> None:
    engine, factory = create_session_factory(isolated_database_url)
    try:
        snapshot_set_id, _, company_code = _seed_monthly_set(engine)
        semantic_version_set_id = _seed_semantic_versions(engine)
        principal = Principal(
            subject="group@example.com",
            roles=frozenset({GROUP_TAX_ROLE}),
            allowed_company_ids=frozenset(),
            organization_path="/group",
        )

        def fail_dispatch(**_kwargs) -> None:
            raise RuntimeError("broker offline")

        app = create_app(
            uow_factory=partial(UnitOfWork, factory),
            settings=Settings(environment="test"),
            principal_provider=lambda _request: principal,
            monthly_semantic_dispatcher=fail_dispatch,
        )
        payload = {
            "monitoring_type": "WELFARE",
            "period": "2026-06",
            "company_codes": [company_code],
            "snapshot_set_id": str(snapshot_set_id),
            "semantic_version_set_id": str(semantic_version_set_id),
        }
        response = TestClient(app).post(
            "/api/v1/monthly-semantic/runs",
            json=payload,
        )
        assert response.status_code == 503
        with engine.connect() as connection:
            failed_run_id, *row = connection.execute(
                text(
                    "SELECT id, status::text, failure_reason, count(*) OVER () "
                    "FROM monitoring_run WHERE semantic_version_set_id = :version_id "
                    "AND snapshot_set_id = :snapshot_set_id"
                ),
                {
                    "version_id": semantic_version_set_id,
                    "snapshot_set_id": snapshot_set_id,
                },
            ).one()
        assert tuple(row) == ("FAILED", "BROKER_DISPATCH_FAILED", 1)

        redispatched: list[UUID] = []
        retry_app = create_app(
            uow_factory=partial(UnitOfWork, factory),
            settings=Settings(environment="test"),
            principal_provider=lambda _request: principal,
            monthly_semantic_dispatcher=lambda **kwargs: redispatched.append(
                kwargs["run_id"]
            ),
        )
        retry = TestClient(retry_app).post(
            "/api/v1/monthly-semantic/runs",
            json=payload,
        )
        assert retry.status_code == 202
        assert retry.json()["run_id"] == str(failed_run_id)
        assert retry.json()["status"] == "RUNNING"
        assert retry.json()["failure_reason"] is None
        assert redispatched == [UUID(retry.json()["run_id"])]
        with engine.connect() as connection:
            assert connection.execute(
                text(
                    "SELECT status::text, failure_reason, count(*) OVER () "
                    "FROM monitoring_run WHERE semantic_version_set_id = :version_id "
                    "AND snapshot_set_id = :snapshot_set_id"
                ),
                {
                    "version_id": semantic_version_set_id,
                    "snapshot_set_id": snapshot_set_id,
                },
            ).one() == ("RUNNING", None, 1)
    finally:
        engine.dispose()


def _seed_semantic_versions(engine: Engine) -> UUID:
    token = uuid4().hex[:12]
    with engine.begin() as connection:
        existing = connection.execute(
            text("SELECT id FROM semantic_version_set LIMIT 1")
        ).scalar_one_or_none()
        if existing is not None:
            return existing
        source_batch_id = connection.execute(
            text(
                "INSERT INTO ingest_batch "
                "(source, source_batch_key, dataset_code, status, extraction_time, period, "
                "mode, schema_version, currency, amount_scale, record_count, accepted_count, "
                "rejected_count, control_total, checksum) "
                "VALUES ('SEMANTIC', :key, 'semantic', 'SUCCEEDED', now(), '2026-06-30', "
                "'FULL', 'v1', 'CNY', 2, 0, 0, 0, 0, repeat('a', 64)) RETURNING id"
            ),
            {"key": f"semantic-{token}"},
        ).scalar_one()
        rule_id = connection.execute(
            text(
                "INSERT INTO rule_version "
                "(rule_code, version, status, effective_from, effective_to, definition, "
                "change_reason, published_at, approved_by) VALUES "
                "('MONTHLY_SEMANTIC', 'monthly-rule-v1', 'PUBLISHED', '2026-01-01', "
                "'2026-12-31', '{}'::jsonb, 'phase 3', now(), 'reviewer') RETURNING id"
            )
        ).scalar_one()
        artifact_ids: dict[str, UUID] = {}
        for artifact_type, version in (
            ("MODEL", "monthly-model-v1"),
            ("PROMPT", "monthly-prompt-v1"),
            ("CASE_LIBRARY", "monthly-cases-v1"),
        ):
            artifact_ids[artifact_type] = connection.execute(
                text(
                    "INSERT INTO semantic_artifact_version "
                    "(artifact_type, version, checksum, storage_ref, deployment_id, "
                    "effective_from, effective_to, status, uploaded_by, reviewer_id, "
                    "published_by, approved_at, published_at) VALUES "
                    "(:type, :version, repeat('b', 64), :storage, :deployment, '2026-01-01', "
                    "'2026-12-31', 'PUBLISHED', 'maker', 'reviewer', 'publisher', now(), now()) "
                    "RETURNING id"
                ),
                {
                    "type": artifact_type,
                    "version": version,
                    "storage": f"governed://{version}",
                    "deployment": "deployment-v1" if artifact_type == "MODEL" else None,
                },
            ).scalar_one()
        dictionary_id = connection.execute(
            text(
                "INSERT INTO suggested_account_dictionary_version "
                "(batch_id, dictionary_version, effective_from, effective_to, checksum, "
                "uploaded_by, reviewer_id, published_by, status, approved_at, published_at) "
                "VALUES (:batch_id, 'candidate-accounts-v2', '2026-01-01', '2026-12-31', "
                "repeat('c', 64), 'maker', 'reviewer', 'publisher', 'PUBLISHED', now(), now()) "
                "RETURNING id"
            ),
            {"batch_id": source_batch_id},
        ).scalar_one()
        return connection.execute(
            text(
                "INSERT INTO semantic_version_set "
                "(set_key, rule_version_id, model_artifact_id, prompt_artifact_id, "
                "case_library_artifact_id, account_dictionary_version_id, effective_from, "
                "effective_to, status) VALUES (repeat('d', 64), :rule_id, :model_id, "
                ":prompt_id, :cases_id, :dictionary_id, '2026-01-01', '2026-12-31', "
                "'PUBLISHED') RETURNING id"
            ),
            {
                "rule_id": rule_id,
                "model_id": artifact_ids["MODEL"],
                "prompt_id": artifact_ids["PROMPT"],
                "cases_id": artifact_ids["CASE_LIBRARY"],
                "dictionary_id": dictionary_id,
            },
        ).scalar_one()
