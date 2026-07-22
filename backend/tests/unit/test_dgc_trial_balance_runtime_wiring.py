from __future__ import annotations

from typing import cast

from fastapi.testclient import TestClient
from pydantic import SecretStr
import pytest

from tax_risk import main as main_module
from tax_risk.adapters.ingest.dgc_sap_profit import DgcClientConfig
from tax_risk.application.dgc_sap_trial_balance import DgcSapTrialBalanceSource
from tax_risk.config import Settings


class _OwnedClient:
    instances: list[_OwnedClient] = []

    def __init__(self, config: DgcClientConfig) -> None:
        self.config = config
        self.closed = False
        self.instances.append(self)

    def close(self) -> None:
        self.closed = True


def test_create_app_owns_and_closes_enabled_trial_balance_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _OwnedClient.instances.clear()
    monkeypatch.setattr(main_module, "DgcSapProfitClient", _OwnedClient)
    settings = Settings(
        dgc_sap_trial_balance_enabled=True,
        dgc_sap_trial_balance_api_url="https://dgc.example.test/trial-balance",
        dgc_sap_trial_balance_app_key=SecretStr("trial-key"),
        dgc_sap_trial_balance_app_secret=SecretStr("trial-secret"),
        dgc_sap_trial_balance_page_size=321,
        dgc_tls_server_name=None,
        dgc_tls_pinned_certificate_sha256=None,
    )

    app = main_module.create_app(settings=settings)

    assert len(_OwnedClient.instances) == 1
    owned = _OwnedClient.instances[0]
    assert app.state.dgc_sap_trial_balance_client is owned
    assert owned.config.api_url == "https://dgc.example.test/trial-balance"
    assert owned.config.app_key == "trial-key"
    assert owned.config.app_secret == "trial-secret"
    assert owned.config.page_size == 321
    with TestClient(app):
        assert owned.closed is False
    assert owned.closed is True


def test_create_app_uses_injected_trial_balance_source_without_owned_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _OwnedClient.instances.clear()
    monkeypatch.setattr(main_module, "DgcSapProfitClient", _OwnedClient)
    source = cast(DgcSapTrialBalanceSource, object())

    app = main_module.create_app(
        settings=Settings(),
        dgc_sap_trial_balance_source=source,
    )

    assert app.state.dgc_sap_trial_balance_client is source
    assert _OwnedClient.instances == []
