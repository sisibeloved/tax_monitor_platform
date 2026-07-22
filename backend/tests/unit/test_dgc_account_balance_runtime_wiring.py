from __future__ import annotations

from typing import cast

from fastapi.testclient import TestClient
from pydantic import SecretStr
import pytest

from tax_risk import main as main_module
from tax_risk.adapters.ingest.dgc_sap_profit import DgcClientConfig
from tax_risk.application.dgc_sap_account_balance import DgcSapAccountBalanceSource
from tax_risk.application.external_fetch import CoordinatedDgcSource
from tax_risk.config import Settings


class _OwnedClient:
    instances: list[_OwnedClient] = []

    def __init__(self, config: DgcClientConfig) -> None:
        self.config = config
        self.closed = False
        self.instances.append(self)

    def close(self) -> None:
        self.closed = True


def test_create_app_owns_and_closes_enabled_account_balance_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _OwnedClient.instances.clear()
    monkeypatch.setattr(main_module, "DgcSapProfitClient", _OwnedClient)
    settings = Settings(
        dgc_sap_account_balance_enabled=True,
        dgc_sap_account_balance_api_url="https://dgc.example.test/account-balance",
        dgc_sap_account_balance_app_key=SecretStr("account-key"),
        dgc_sap_account_balance_app_secret=SecretStr("account-secret"),
        dgc_sap_account_balance_page_size=432,
        dgc_tls_server_name=None,
        dgc_tls_pinned_certificate_sha256=None,
    )

    app = main_module.create_app(settings=settings)

    assert len(_OwnedClient.instances) == 1
    owned = _OwnedClient.instances[0]
    assert app.state.dgc_sap_account_balance_client is owned
    assert owned.config.api_url == "https://dgc.example.test/account-balance"
    assert owned.config.app_key == "account-key"
    assert owned.config.app_secret == "account-secret"
    assert owned.config.page_size == 432
    with TestClient(app):
        assert owned.closed is False
    assert owned.closed is True


def test_injected_account_balance_source_uses_parallel_fetch_coordinator() -> None:
    source = cast(DgcSapAccountBalanceSource, object())
    app = main_module.create_app(
        settings=Settings(
            external_fetch_enabled=True,
            external_fetch_cache_enabled=False,
        ),
        dgc_sap_account_balance_source=source,
    )

    assert isinstance(app.state.dgc_sap_account_balance_client, CoordinatedDgcSource)
    assert app.state.external_fetch_coordinator is not None
