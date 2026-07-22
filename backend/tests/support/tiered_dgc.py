"""Two-tier DGC test sources with explicit REAL/MOCK provenance."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from pydantic import SecretStr

from tax_risk.adapters.ingest.dgc_sap_profit import (
    DgcClientConfig,
    DgcFetchResult,
    DgcSapProfitClient,
)
from tax_risk.config import Settings


class SourceMode(StrEnum):
    REAL = "REAL"
    MOCK = "MOCK"


class DataStatus(StrEnum):
    DATA = "DATA"
    NO_DATA = "NO_DATA"


class DgcInterface(StrEnum):
    SAP_PROFIT = "sap_profit"
    SAP_TRIAL_BALANCE = "sap_trial_balance"
    SAP_ACCOUNT_BALANCE = "sap_account_balance"
    HESI_REIMBURSEMENT = "hesi_reimbursement"
    HESI_INVOICE = "hesi_invoice"
    SAP_DIVIDEND_DETAIL = "sap_dividend_detail"
    INVOICE_DETAIL = "invoice_detail"


class TieredConfigurationError(ValueError):
    """Raised when credentials are present but cannot form a real connection."""


class DgcSource(Protocol):
    def fetch(self, parameters: Mapping[str, object]) -> DgcFetchResult: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class TieredDgcConfig:
    interface: DgcInterface
    api_url: str | None
    page_size: int
    timeout: float
    max_pages: int
    max_records: int
    max_page_bytes: int
    max_total_bytes: int
    token_ttl: float
    tls_server_name: str | None
    tls_pinned_certificate_sha256: str | None
    app_key: str | None = field(default=None, repr=False)
    app_secret: str | None = field(default=None, repr=False)
    iam_url: str | None = None
    iam_username: str | None = None
    iam_password: str | None = field(default=None, repr=False)
    iam_domain: str | None = None
    iam_project: str | None = None

    def real_client_config(self) -> DgcClientConfig | None:
        """Build real config, return None for a genuinely unconfigured source."""

        app_values = (self.app_key, self.app_secret)
        iam_values = (
            self.iam_url,
            self.iam_username,
            self.iam_password,
            self.iam_domain,
            self.iam_project,
        )
        app_present = tuple(_nonempty(value) for value in app_values)
        iam_present = tuple(_nonempty(value) for value in iam_values)
        if any(app_present) and not all(app_present):
            raise TieredConfigurationError(
                f"{self.interface.value}: AppKey and AppSecret must both be configured"
            )
        if any(iam_present) and not all(iam_present):
            raise TieredConfigurationError(
                f"{self.interface.value}: all IAM credentials must be configured"
            )
        app_configured = all(app_present)
        iam_configured = all(iam_present)
        if app_configured and iam_configured:
            raise TieredConfigurationError(
                f"{self.interface.value}: configure exactly one authentication method"
            )
        if not app_configured and not iam_configured:
            return None
        if not _nonempty(self.api_url):
            raise TieredConfigurationError(
                f"{self.interface.value}: credentials require an API URL"
            )

        assert self.api_url is not None
        if app_configured:
            return DgcClientConfig(
                api_url=self.api_url,
                app_key=self.app_key,
                app_secret=self.app_secret,
                timeout=self.timeout,
                page_size=self.page_size,
                max_pages=self.max_pages,
                max_records=self.max_records,
                max_page_bytes=self.max_page_bytes,
                max_total_bytes=self.max_total_bytes,
                token_ttl=self.token_ttl,
                tls_server_name=self.tls_server_name,
                tls_pinned_certificate_sha256=self.tls_pinned_certificate_sha256,
            )
        return DgcClientConfig(
            api_url=self.api_url,
            iam_url=self.iam_url,
            username=self.iam_username,
            password=self.iam_password,
            domain=self.iam_domain,
            project=self.iam_project,
            timeout=self.timeout,
            page_size=self.page_size,
            max_pages=self.max_pages,
            max_records=self.max_records,
            max_page_bytes=self.max_page_bytes,
            max_total_bytes=self.max_total_bytes,
            token_ttl=self.token_ttl,
            tls_server_name=self.tls_server_name,
            tls_pinned_certificate_sha256=self.tls_pinned_certificate_sha256,
        )


@dataclass(slots=True)
class StaticDgcSource:
    """Deterministic fallback used only when real configuration is absent."""

    result: DgcFetchResult
    calls: list[Mapping[str, object]] = field(default_factory=list)

    def fetch(self, parameters: Mapping[str, object]) -> DgcFetchResult:
        self.calls.append(dict(parameters))
        return self.result

    def close(self) -> None:
        return None


@dataclass(slots=True)
class TieredDgcSource:
    interface: DgcInterface
    mode: SourceMode
    _source: DgcSource = field(repr=False)

    def fetch(self, parameters: Mapping[str, object]) -> DgcFetchResult:
        # Deliberately do not catch real-source failures: an outage must fail the test.
        return self._source.fetch(parameters)

    def close(self) -> None:
        self._source.close()

    def __enter__(self) -> TieredDgcSource:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: object | None,
    ) -> None:
        self.close()


def load_tiered_settings(repo_root: Path | None = None) -> Settings:
    """Load process environment first, then the repository's local infra/.env."""

    root = repo_root or Path(__file__).resolve().parents[3]
    env_file = root / "infra" / ".env"
    if env_file.is_file():
        return Settings(  # type: ignore[call-arg]
            _env_file=env_file,
            _env_file_encoding="utf-8",
        )
    return Settings(_env_file=None)  # type: ignore[call-arg]


def tiered_config(settings: Settings, interface: DgcInterface) -> TieredDgcConfig:
    """Map one application's interface settings without consulting enabled flags."""

    api_url: str | None
    app_key: SecretStr | None
    app_secret: SecretStr | None
    page_size: int
    iam_url: str | None = None
    iam_username: str | None = None
    iam_password: SecretStr | None = None
    iam_domain: str | None = None
    iam_project: str | None = None

    if interface is DgcInterface.SAP_PROFIT:
        api_url = settings.dgc_sap_profit_api_url
        app_key = settings.dgc_app_key
        app_secret = settings.dgc_app_secret
        page_size = settings.dgc_page_size
        iam_url = settings.dgc_iam_url if settings.dgc_iam_username else None
        iam_username = settings.dgc_iam_username
        iam_password = settings.dgc_iam_password
        iam_domain = settings.dgc_iam_domain if settings.dgc_iam_username else None
        iam_project = settings.dgc_iam_project if settings.dgc_iam_username else None
    elif interface is DgcInterface.SAP_TRIAL_BALANCE:
        api_url = settings.dgc_sap_trial_balance_api_url
        app_key = settings.dgc_sap_trial_balance_app_key
        app_secret = settings.dgc_sap_trial_balance_app_secret
        page_size = settings.dgc_sap_trial_balance_page_size
    elif interface is DgcInterface.SAP_ACCOUNT_BALANCE:
        api_url = settings.dgc_sap_account_balance_api_url
        app_key = settings.dgc_sap_account_balance_app_key
        app_secret = settings.dgc_sap_account_balance_app_secret
        page_size = settings.dgc_sap_account_balance_page_size
    elif interface is DgcInterface.HESI_REIMBURSEMENT:
        api_url = settings.dgc_hesi_reimbursement_api_url
        app_key = settings.dgc_hesi_reimbursement_app_key
        app_secret = settings.dgc_hesi_reimbursement_app_secret
        page_size = settings.dgc_hesi_reimbursement_page_size
    elif interface is DgcInterface.HESI_INVOICE:
        api_url = settings.dgc_hesi_invoice_api_url
        app_key = settings.dgc_hesi_invoice_app_key
        app_secret = settings.dgc_hesi_invoice_app_secret
        page_size = settings.dgc_hesi_invoice_page_size
    elif interface is DgcInterface.SAP_DIVIDEND_DETAIL:
        api_url = settings.dgc_sap_dividend_detail_api_url
        app_key = settings.dgc_sap_dividend_detail_app_key
        app_secret = settings.dgc_sap_dividend_detail_app_secret
        page_size = settings.dgc_sap_dividend_detail_page_size
    else:
        api_url = settings.dgc_invoice_detail_api_url
        app_key = settings.dgc_invoice_detail_app_key
        app_secret = settings.dgc_invoice_detail_app_secret
        page_size = settings.dgc_invoice_detail_page_size

    return TieredDgcConfig(
        interface=interface,
        api_url=api_url,
        app_key=_secret_value(app_key),
        app_secret=_secret_value(app_secret),
        iam_url=iam_url,
        iam_username=iam_username,
        iam_password=_secret_value(iam_password),
        iam_domain=iam_domain,
        iam_project=iam_project,
        page_size=page_size,
        timeout=settings.dgc_timeout_seconds,
        max_pages=settings.dgc_max_pages,
        max_records=settings.dgc_max_records,
        max_page_bytes=settings.dgc_max_page_bytes,
        max_total_bytes=settings.dgc_max_total_bytes,
        token_ttl=settings.dgc_token_ttl_seconds,
        tls_server_name=settings.dgc_tls_server_name,
        tls_pinned_certificate_sha256=settings.dgc_tls_pinned_certificate_sha256,
    )


def build_tiered_source(
    config: TieredDgcConfig,
    mock_result: DgcFetchResult,
    *,
    real_source_factory: Callable[[DgcClientConfig], DgcSource] = DgcSapProfitClient,
) -> TieredDgcSource:
    """Select REAL when config is complete; otherwise use deterministic MOCK."""

    client_config = config.real_client_config()
    if client_config is None:
        return TieredDgcSource(
            interface=config.interface,
            mode=SourceMode.MOCK,
            _source=StaticDgcSource(mock_result),
        )
    return TieredDgcSource(
        interface=config.interface,
        mode=SourceMode.REAL,
        _source=real_source_factory(client_config),
    )


def data_status(result: DgcFetchResult) -> DataStatus:
    return DataStatus.DATA if result.records else DataStatus.NO_DATA


def source_report(source: TieredDgcSource, result: DgcFetchResult) -> str:
    """Return a credential-free line suitable for terminal and JUnit output."""

    return (
        f"TIERED_INTERFACE source={source.interface.value} mode={source.mode.value} "
        f"data_status={data_status(result).value} records={len(result.records)}"
    )


def _secret_value(value: SecretStr | None) -> str | None:
    return value.get_secret_value() if value is not None else None


def _nonempty(value: object) -> bool:
    return isinstance(value, str) and bool(value.strip())


__all__ = [
    "DataStatus",
    "DgcInterface",
    "DgcSource",
    "SourceMode",
    "StaticDgcSource",
    "TieredConfigurationError",
    "TieredDgcConfig",
    "TieredDgcSource",
    "build_tiered_source",
    "data_status",
    "load_tiered_settings",
    "source_report",
    "tiered_config",
]
