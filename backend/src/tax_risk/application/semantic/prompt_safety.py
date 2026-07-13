"""Deterministic field allow-listing and PII minimization before model calls."""

from __future__ import annotations

import re


_PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_IDENTITY = re.compile(r"(?<!\d)\d{17}[\dXx](?![\dXx])")
_NAMED_PERSON = re.compile(r"(?:联系人|参与人|姓名)[:：]?\s*[\u4e00-\u9fff]{2,4}")
_FORBIDDEN_KEY_PARTS = (
    "name",
    "phone",
    "mobile",
    "identity",
    "id_card",
    "身份证",
    "姓名",
    "电话",
)


def minimize_model_input(
    payload: dict[str, object],
    *,
    allowed_fields: frozenset[str],
) -> dict[str, object]:
    minimized: dict[str, object] = {}
    for key in sorted(allowed_fields):
        if key not in payload or _forbidden_key(key):
            continue
        value = payload[key]
        if isinstance(value, str):
            minimized[key] = _redact_text(value)
        elif isinstance(value, (int, float, bool)) or value is None:
            minimized[key] = value
    return minimized


def _forbidden_key(key: str) -> bool:
    normalized = key.casefold()
    return any(part in normalized for part in _FORBIDDEN_KEY_PARTS)


def _redact_text(value: str) -> str:
    redacted = _NAMED_PERSON.sub("[姓名已脱敏]", value)
    redacted = _PHONE.sub("[电话已脱敏]", redacted)
    return _IDENTITY.sub("[证件已脱敏]", redacted)


__all__ = ["minimize_model_input"]
