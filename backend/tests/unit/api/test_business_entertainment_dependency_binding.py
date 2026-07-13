from __future__ import annotations

import pytest

from tax_risk.api.business_entertainment_dependencies import (
    BusinessEntertainmentDependencyError,
    bind_structured_model_client,
)
from tax_risk.config import Settings
from tax_risk.model_gateway.service import ProtectedModelGateway


def test_only_explicit_test_configuration_binds_protected_fake_gateway() -> None:
    client = bind_structured_model_client(
        Settings(environment="test", semantic_model_provider="fake"),
        credential_resolver=lambda _reference: "unused",
    )

    assert isinstance(client, ProtectedModelGateway)


def test_production_never_falls_back_to_fake_and_fails_closed_when_incomplete() -> None:
    with pytest.raises(BusinessEntertainmentDependencyError, match="incomplete"):
        bind_structured_model_client(
            Settings(
                environment="production",
                semantic_model_provider="enterprise",
                export_download_secret="test-production-export-secret-32-chars",
                worker_scope_secret="test-production-worker-secret-32-chars",
            ),
            credential_resolver=lambda _reference: "token",
        )
    with pytest.raises(BusinessEntertainmentDependencyError, match="fake"):
        bind_structured_model_client(
            Settings(
                environment="production",
                semantic_model_provider="fake",
                export_download_secret="test-production-export-secret-32-chars",
                worker_scope_secret="test-production-worker-secret-32-chars",
            ),
            credential_resolver=lambda _reference: "token",
        )


def test_complete_production_configuration_binds_protected_enterprise_gateway() -> None:
    client = bind_structured_model_client(
        Settings(
            environment="production",
            semantic_model_provider="enterprise",
            semantic_model_endpoint="https://model.internal.example/generate",
            semantic_model_deployment="income-tax-v1",
            semantic_model_credential_ref="secret://income-tax-model",
            semantic_model_zero_retention_required=True,
            export_download_secret="test-production-export-secret-32-chars",
            worker_scope_secret="test-production-worker-secret-32-chars",
        ),
        credential_resolver=lambda _reference: "token",
    )

    assert isinstance(client, ProtectedModelGateway)


def test_production_rejects_provider_that_may_train_on_enterprise_data() -> None:
    with pytest.raises(BusinessEntertainmentDependencyError, match="public-training"):
        bind_structured_model_client(
            Settings(
                environment="production",
                semantic_model_provider="enterprise",
                semantic_model_endpoint="https://model.internal.example/generate",
                semantic_model_deployment="income-tax-v1",
                semantic_model_credential_ref="secret://income-tax-model",
                semantic_model_no_public_training=False,
                export_download_secret="test-production-export-secret-32-chars",
                worker_scope_secret="test-production-worker-secret-32-chars",
            ),
            credential_resolver=lambda _reference: "token",
        )
