from __future__ import annotations

from typing import cast

from fastapi.testclient import TestClient
import pytest

from tax_risk import main as main_module
from tax_risk.adapters.ingest.dgc_sap_profit import DgcClientConfig
from tax_risk.application.dgc_invoice_detail import DgcInvoiceDetailSource
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


def test_create_app_owns_and_closes_enabled_invoice_detail_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _OwnedClient.instances.clear()
    monkeypatch.setattr(main_module, "DgcSapProfitClient", _OwnedClient)
    settings = Settings(
        dgc_invoice_detail_enabled=True,
        dgc_invoice_detail_api_url="https://dgc.example.test/invoice-detail",
        dgc_invoice_detail_app_key="invoice-key",
        dgc_invoice_detail_app_secret="invoice-secret",
        dgc_invoice_detail_page_size=654,
        dgc_tls_server_name=None,
        dgc_tls_pinned_certificate_sha256=None,
    )

    app = main_module.create_app(settings=settings)

    assert len(_OwnedClient.instances) == 1
    owned = _OwnedClient.instances[0]
    assert app.state.dgc_invoice_detail_client is owned
    assert owned.config.api_url == "https://dgc.example.test/invoice-detail"
    assert owned.config.app_key == "invoice-key"
    assert owned.config.app_secret == "invoice-secret"
    assert owned.config.page_size == 654
    with TestClient(app):
        assert owned.closed is False
    assert owned.closed is True


def test_injected_invoice_detail_source_uses_parallel_fetch_coordinator() -> None:
    source = cast(DgcInvoiceDetailSource, object())
    app = main_module.create_app(
        settings=Settings(
            external_fetch_enabled=True,
            external_fetch_cache_enabled=False,
        ),
        dgc_invoice_detail_source=source,
    )

    assert isinstance(app.state.dgc_invoice_detail_client, CoordinatedDgcSource)
    assert app.state.external_fetch_coordinator is not None
