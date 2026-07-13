from __future__ import annotations

from collections.abc import Iterator
from datetime import date
from functools import partial

import pytest

from tax_risk.application.semantic.version_registry import (
    ArtifactStatus,
    ArtifactType,
    SemanticArtifactConflictError,
    SemanticArtifactNotReadyError,
    SemanticVersionRegistry,
)
from tax_risk.persistence.repositories import UnitOfWork, create_session_factory


@pytest.fixture
def registry_resources(
    isolated_database_url: str,
) -> Iterator[SemanticVersionRegistry]:
    engine, factory = create_session_factory(isolated_database_url)
    try:
        yield SemanticVersionRegistry(partial(UnitOfWork, factory))
    finally:
        engine.dispose()


def test_artifacts_require_independent_review_and_published_effective_versions(
    registry_resources: SemanticVersionRegistry,
) -> None:
    registry = registry_resources
    model = registry.create(
        artifact_type=ArtifactType.MODEL,
        version="model-v1",
        checksum="1" * 64,
        storage_ref="enterprise://models/model-v1",
        deployment_id="income-tax-semantic-v1",
        effective_from=date(2029, 1, 1),
        effective_to=date(2029, 12, 31),
        uploaded_by="maker@example.com",
    )
    prompt = registry.create(
        artifact_type=ArtifactType.PROMPT,
        version="prompt-v1",
        checksum="2" * 64,
        storage_ref="controlled://prompts/prompt-v1",
        deployment_id=None,
        effective_from=date(2029, 1, 1),
        effective_to=date(2029, 12, 31),
        uploaded_by="maker@example.com",
    )
    cases = registry.create(
        artifact_type=ArtifactType.CASE_LIBRARY,
        version="cases-v1",
        checksum="3" * 64,
        storage_ref="controlled://cases/cases-v1",
        deployment_id=None,
        effective_from=date(2029, 1, 1),
        effective_to=date(2029, 12, 31),
        uploaded_by="maker@example.com",
    )

    with pytest.raises(SemanticArtifactConflictError, match="different"):
        registry.approve(model.artifact_id, reviewed_by="maker@example.com")
    with pytest.raises(SemanticArtifactNotReadyError, match="published"):
        registry.resolve_active(
            effective_on=date(2029, 3, 31),
            rule_version_id="rule-v1",
            account_dictionary_version="accounts-v1",
        )

    for artifact in (model, prompt, cases):
        approved = registry.approve(
            artifact.artifact_id,
            reviewed_by="reviewer@example.com",
        )
        assert approved.status is ArtifactStatus.APPROVED
        published = registry.publish(
            artifact.artifact_id,
            published_by="reviewer@example.com",
        )
        assert published.status is ArtifactStatus.PUBLISHED
        assert published.published_at is not None

    versions = registry.resolve_active(
        effective_on=date(2029, 3, 31),
        rule_version_id="rule-v1",
        account_dictionary_version="accounts-v1",
    )
    assert versions.model_version_id == "model-v1"
    assert versions.prompt_version_id == "prompt-v1"
    assert versions.case_library_version_id == "cases-v1"


def test_published_artifact_periods_cannot_overlap(
    registry_resources: SemanticVersionRegistry,
) -> None:
    registry = registry_resources
    first = registry.create(
        artifact_type=ArtifactType.MODEL,
        version="model-overlap-a",
        checksum="a" * 64,
        storage_ref="enterprise://models/a",
        deployment_id="a",
        effective_from=date(2030, 1, 1),
        effective_to=date(2030, 12, 31),
        uploaded_by="maker-a",
    )
    second = registry.create(
        artifact_type=ArtifactType.MODEL,
        version="model-overlap-b",
        checksum="b" * 64,
        storage_ref="enterprise://models/b",
        deployment_id="b",
        effective_from=date(2030, 6, 1),
        effective_to=date(2031, 5, 31),
        uploaded_by="maker-b",
    )
    for artifact in (first, second):
        registry.approve(artifact.artifact_id, reviewed_by="reviewer")
    registry.publish(first.artifact_id, published_by="reviewer")
    with pytest.raises(SemanticArtifactConflictError, match="overlap"):
        registry.publish(second.artifact_id, published_by="reviewer")
