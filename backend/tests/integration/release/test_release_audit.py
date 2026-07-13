from datetime import datetime, timezone
from functools import partial

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError

from tax_risk.persistence.models import ReleaseEvent, ReleaseManifestRecord
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory
from tax_risk.release.manifest import ReleaseArtifacts, ReleaseManifest
from tax_risk.release.reporting import SqlReleaseStore
from tax_risk.release.signing import SignatureEnvelope


def _manifest() -> ReleaseManifest:
    return ReleaseManifest(
        candidate_version="2026.07.13-rc1",
        application_image_digest=f"sha256:{'a' * 64}",
        git_commit="b" * 40,
        migration_head="0016_release_manifests",
        artifacts=ReleaseArtifacts(
            rule_package_sha256="1" * 64,
            prompt_package_sha256="2" * 64,
            model_adapter_config_sha256="3" * 64,
            account_dictionary_sha256="4" * 64,
            case_library_sha256="5" * 64,
            evaluation_report_sha256="6" * 64,
            replay_report_sha256="7" * 64,
        ),
        created_at=datetime(2026, 7, 13, 8, tzinfo=timezone.utc),
    )


def test_release_lifecycle_persists_hash_only_audit_evidence(
    isolated_database_url: str,
) -> None:
    engine, factory = create_session_factory(isolated_database_url)
    store = SqlReleaseStore(partial(UnitOfWork, factory))
    manifest = _manifest()
    signature = SignatureEnvelope(
        manifest_sha256=manifest.manifest_sha256,
        key_id="prod-release-key",
        key_version="v9",
        algorithm="ED25519",
        signature_base64="c2lnbmF0dXJl",
        signed_at=datetime(2026, 7, 13, 9, tzinfo=timezone.utc),
    )
    try:
        release_id = store.create_candidate(manifest, actor="release-bot")
        store.record_replay_started(release_id, actor="release-bot")
        store.record_replay_result(
            release_id,
            report_sha256="7" * 64,
            approved=True,
            actor="release-bot",
        )
        store.approve(release_id, approver="tax-owner@example.com")
        store.attach_signature(release_id, signature, actor="kms-workload")
        store.record_verification(release_id, actor="release-verifier")
        store.promote(release_id, approver="operations-owner@example.com")

        with factory() as session:
            record = session.get(ReleaseManifestRecord, release_id)
            events = list(
                session.scalars(
                    select(ReleaseEvent)
                    .where(ReleaseEvent.manifest_id == release_id)
                    .order_by(ReleaseEvent.occurred_at, ReleaseEvent.id)
                )
            )
        assert record is not None
        assert record.status == "PROMOTED"
        assert record.manifest_sha256 == manifest.manifest_sha256
        assert record.replay_report_sha256 == "7" * 64
        assert record.signer_key_id == "prod-release-key"
        assert [event.action for event in events] == [
            "CANDIDATE_CREATED",
            "REPLAY_STARTED",
            "REPLAY_APPROVED",
            "RELEASE_APPROVED",
            "MANIFEST_SIGNED",
            "SIGNATURE_VERIFIED",
            "RELEASE_PROMOTED",
        ]
        assert all(event.manifest_sha256 == manifest.manifest_sha256 for event in events)
        assert all(
            event.approver is not None
            for event in events
            if event.action in {"RELEASE_APPROVED", "RELEASE_PROMOTED"}
        )
    finally:
        engine.dispose()


def test_release_lifecycle_events_are_database_append_only(
    isolated_database_url: str,
) -> None:
    engine, factory = create_session_factory(isolated_database_url)
    store = SqlReleaseStore(partial(UnitOfWork, factory))
    try:
        manifest = _manifest().model_copy(update={"candidate_version": "2026.07.13-immutable"})
        release_id = store.create_candidate(manifest, actor="release-bot-immutable")
        with factory() as session:
            event_id = session.scalar(
                select(ReleaseEvent.id).where(ReleaseEvent.manifest_id == release_id)
            )
        assert event_id is not None

        with pytest.raises(DBAPIError, match="append-only"):
            with engine.begin() as connection:
                connection.execute(
                    text("UPDATE release_event SET action = 'REPLAY_STARTED' WHERE id = :id"),
                    {"id": event_id},
                )
        with pytest.raises(DBAPIError, match="append-only"):
            with engine.begin() as connection:
                connection.execute(
                    text("DELETE FROM release_event WHERE id = :id"),
                    {"id": event_id},
                )
    finally:
        engine.dispose()
