from __future__ import annotations

import pytest

from tax_risk.adapters.model.enterprise_structured_client import (
    EnterpriseStructuredModelClient,
)
from tax_risk.adapters.model.fake_structured_client import FakeStructuredModelClient
from tax_risk.api.business_entertainment_dependencies import (
    BusinessEntertainmentDependencyError,
    bind_structured_model_client,
)
from tax_risk.config import Settings


def test_only_explicit_test_configuration_binds_fake_client() -> None:
    client = bind_structured_model_client(
        Settings(environment="test", semantic_model_provider="fake"),
        credential_resolver=lambda _reference: "unused",
    )

    assert isinstance(client, FakeStructuredModelClient)


def test_production_never_falls_back_to_fake_and_fails_closed_when_incomplete() -> None:
    with pytest.raises(BusinessEntertainmentDependencyError, match="incomplete"):
        bind_structured_model_client(
            Settings(environment="production", semantic_model_provider="enterprise"),
            credential_resolver=lambda _reference: "token",
        )
    with pytest.raises(BusinessEntertainmentDependencyError, match="fake"):
        bind_structured_model_client(
            Settings(environment="production", semantic_model_provider="fake"),
            credential_resolver=lambda _reference: "token",
        )


def test_complete_production_configuration_binds_enterprise_client() -> None:
    client = bind_structured_model_client(
        Settings(
            environment="production",
            semantic_model_provider="enterprise",
            semantic_model_endpoint="https://model.internal.example/generate",
            semantic_model_deployment="income-tax-v1",
            semantic_model_credential_ref="secret://income-tax-model",
            semantic_model_zero_retention_required=True,
        ),
        credential_resolver=lambda _reference: "token",
    )

    assert isinstance(client, EnterpriseStructuredModelClient)
    assert not isinstance(client, FakeStructuredModelClient)
