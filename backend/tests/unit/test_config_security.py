from __future__ import annotations

import pytest

from tax_risk.config import Settings


@pytest.mark.parametrize(
    "overrides",
    (
        {},
        {"export_download_secret": "development-export-download-secret"},
        {"worker_scope_secret": "development-worker-scope-secret-change-me"},
    ),
)
def test_production_rejects_default_runtime_signing_secrets(
    overrides: dict[str, str],
) -> None:
    values = {
        "environment": "production",
        "export_download_secret": "production-export-secret-at-least-32-chars",
        "worker_scope_secret": "production-worker-secret-at-least-32-chars",
    }
    values.update(overrides)
    if not overrides:
        values.pop("export_download_secret")
        values.pop("worker_scope_secret")

    with pytest.raises(ValueError, match="production runtime secrets"):
        Settings(**values)  # type: ignore[arg-type]


def test_production_accepts_independent_nondefault_runtime_signing_secrets() -> None:
    settings = Settings(
        environment="production",
        export_download_secret="production-export-secret-at-least-32-chars",
        worker_scope_secret="production-worker-secret-at-least-32-chars",
    )

    assert settings.export_download_secret != settings.worker_scope_secret
