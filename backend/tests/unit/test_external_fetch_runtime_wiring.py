from __future__ import annotations

from collections.abc import Mapping
from hashlib import sha256

from fastapi.testclient import TestClient
import pytest

from tax_risk.adapters.cache.memory_fetch_cache import MemoryFetchCache
from tax_risk.adapters.ingest.dgc_sap_profit import DgcFetchResult
from tax_risk.application.external_fetch import (
    CoordinatedDgcSource,
    FetchCoordinatorClosedError,
)
from tax_risk.config import Settings
from tax_risk.main import create_app
from tax_risk.observability.metrics import build_default_registry


class Source:
    def __init__(self) -> None:
        self.calls = 0

    def fetch(self, parameters: Mapping[str, object]) -> DgcFetchResult:
        self.calls += 1
        return DgcFetchResult(
            records=({"company": parameters["company"]},),
            checksum=sha256(
                f'[{ {"company": parameters["company"]} }]'.encode()
            ).hexdigest(),
        )


def test_create_app_wraps_sources_with_shared_coordinator_and_closes_it() -> None:
    source = Source()
    settings = Settings(
        external_fetch_enabled=True,
        external_fetch_cache_enabled=False,
    )
    registry = build_default_registry()
    app = create_app(
        settings=settings,
        dgc_sap_profit_source=source,
        external_fetch_cache=MemoryFetchCache(),
        metrics_registry=registry,
    )

    assert isinstance(app.state.dgc_sap_profit_client, CoordinatedDgcSource)
    assert app.state.external_fetch_coordinator is not None
    with TestClient(app):
        app.state.dgc_sap_profit_client.fetch({"company": "3000"})
        app.state.dgc_sap_profit_client.fetch({"company": "3000"})
        assert source.calls == 1
        metrics = registry.render_prometheus()
        assert 'provenance="LIVE"' in metrics
        assert 'provenance="CACHE"' in metrics
        assert 'source="dgc_sap_profit"' in metrics

    with pytest.raises(FetchCoordinatorClosedError):
        app.state.dgc_sap_profit_client.fetch({"company": "3000"})
