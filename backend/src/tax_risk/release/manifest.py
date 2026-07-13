from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json

from pydantic import BaseModel, ConfigDict, Field, field_validator


SHA256_PATTERN = r"^[0-9a-f]{64}$"


class ReleaseArtifacts(BaseModel):
    """发布候选必须锁定的全部治理制品摘要。"""

    rule_package_sha256: str = Field(pattern=SHA256_PATTERN)
    prompt_package_sha256: str = Field(pattern=SHA256_PATTERN)
    model_adapter_config_sha256: str = Field(pattern=SHA256_PATTERN)
    account_dictionary_sha256: str = Field(pattern=SHA256_PATTERN)
    case_library_sha256: str = Field(pattern=SHA256_PATTERN)
    evaluation_report_sha256: str = Field(pattern=SHA256_PATTERN)
    replay_report_sha256: str = Field(pattern=SHA256_PATTERN)

    model_config = ConfigDict(extra="forbid", frozen=True)


class ReleaseManifest(BaseModel):
    """可重复序列化、可签名的发布候选清单。"""

    candidate_version: str = Field(min_length=1, max_length=128)
    application_image_digest: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    git_commit: str = Field(pattern=r"^[0-9a-f]{40,64}$")
    migration_head: str = Field(min_length=1, max_length=128)
    artifacts: ReleaseArtifacts
    created_at: datetime

    model_config = ConfigDict(extra="forbid", frozen=True)

    @field_validator("created_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("created_at must include a timezone")
        return value

    def canonical_bytes(self) -> bytes:
        """Return stable UTF-8 JSON independent of insertion order and whitespace."""

        payload = self.model_dump(mode="json")
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    @property
    def manifest_sha256(self) -> str:
        return sha256(self.canonical_bytes()).hexdigest()


__all__ = ["ReleaseArtifacts", "ReleaseManifest", "SHA256_PATTERN"]
