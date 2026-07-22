from __future__ import annotations

from typing import cast

from fastapi.testclient import TestClient
import pytest

from tax_risk.adapters.ingest.dgc_sap_profit import DgcClientConfig
from tax_risk.application.dgc_sap_dividend_detail import DgcSapDividendDetailSource
from tax_risk.config import Settings
from tax_risk import main as main_module


class _OwnedClient:
    instances: list[_OwnedClient] = []

    def __init__(self, config: DgcClientConfig) -> None:
        self.config = config
        self.closed = False
        self.instances.append(self)

    def close(self) -> None:
        self.closed = True


def test_create_app_owns_and_closes_enabled_dividend_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _OwnedClient.instances.clear()
    monkeypatch.setattr(main_module, "DgcSapProfitClient", _OwnedClient)
    settings = Settings(
        dgc_sap_dividend_detail_enabled=True,
        dgc_sap_dividend_detail_api_url="https://dgc.example.test/dividend",
        dgc_sap_dividend_detail_app_key="dividend-key",
        dgc_sap_dividend_detail_app_secret="dividend-secret",
        dgc_sap_dividend_detail_page_size=321,
        dgc_tls_server_name=None,
        dgc_tls_pinned_certificate_sha256=None,
    )

    app = main_module.create_app(settings=settings)

    assert len(_OwnedClient.instances) == 1
    owned = _OwnedClient.instances[0]
    assert app.state.dgc_sap_dividend_detail_client is owned
    assert owned.config.api_url == "https://dgc.example.test/dividend"
    assert owned.config.app_key == "dividend-key"
    assert owned.config.app_secret == "dividend-secret"
    assert owned.config.page_size == 321
    with TestClient(app):
        assert owned.closed is False
    assert owned.closed is True


def test_create_app_uses_injected_dividend_source_without_owned_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _OwnedClient.instances.clear()
    monkeypatch.setattr(main_module, "DgcSapProfitClient", _OwnedClient)
    source = cast(DgcSapDividendDetailSource, object())

    app = main_module.create_app(
        settings=Settings(),
        dgc_sap_dividend_detail_source=source,
    )

    assert app.state.dgc_sap_dividend_detail_client is source
    assert _OwnedClient.instances == []
