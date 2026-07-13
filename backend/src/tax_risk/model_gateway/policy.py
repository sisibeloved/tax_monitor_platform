"""Model provider admission and deterministic payload minimization."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping


_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_BANK_ACCOUNT = re.compile(r"(?<!\d)\d{16,19}(?!\d)")
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
_ATTACHMENT_URL = re.compile(r"https?://\S+")
_TOP_LEVEL_FIELDS = frozenset(
    {
        "account_dictionary_version",
        "amount",
        "counterparty_type",
        "current_account_name",
        "document_date",
        "evidence",
        "participant_categories",
        "reference_snippet",
        "scenario",
    }
)
_EVIDENCE_FIELDS = frozenset({"evidence_id", "field_name", "value"})


class UnsafeModelConfiguration(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class ProviderPolicy:
    environment: str
    no_public_training: bool
    retention_mode: str


class ModelGatewayPolicy:
    def __init__(self, provider: ProviderPolicy) -> None:
        if provider.environment == "production":
            if not provider.no_public_training:
                raise UnsafeModelConfiguration(
                    "production model provider must prohibit public training"
                )
            if provider.retention_mode not in {"zero", "approved"}:
                raise UnsafeModelConfiguration(
                    "production model retention must be zero or explicitly approved"
                )
        self.provider = provider

    def prepare_payload(self, payload: Mapping[str, Any]) -> dict[str, object]:
        prepared: dict[str, object] = {}
        for key in sorted(_TOP_LEVEL_FIELDS & payload.keys()):
            value = payload[key]
            if key == "evidence":
                prepared[key] = self._prepare_evidence(value)
            else:
                prepared[key] = _sanitize_value(value)
        return prepared

    @staticmethod
    def _prepare_evidence(value: Any) -> list[dict[str, object]]:
        if not isinstance(value, list):
            return []
        evidence: list[dict[str, object]] = []
        for item in value:
            if not isinstance(item, Mapping):
                continue
            evidence.append(
                {
                    key: _sanitize_value(item[key])
                    for key in sorted(_EVIDENCE_FIELDS & item.keys())
                }
            )
        return evidence


def _sanitize_value(value: Any) -> object:
    if isinstance(value, str):
        redacted = _ATTACHMENT_URL.sub("[附件引用已移除]", value)
        redacted = _EMAIL.sub("[邮箱已脱敏]", redacted)
        redacted = _BANK_ACCOUNT.sub("[银行账号已脱敏]", redacted)
        return _PHONE.sub("[电话已脱敏]", redacted)
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    return str(value)


__all__ = [
    "ModelGatewayPolicy",
    "ProviderPolicy",
    "UnsafeModelConfiguration",
]
