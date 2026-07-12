from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-backed application settings."""

    database_url: str = "postgresql+psycopg://tax_risk:tax_risk@localhost:5432/tax_risk"
    redis_url: str = "redis://localhost:6379/0"
    environment: Literal["development", "test", "production"] = "development"
    development_principal_enabled: bool = False
    ingest_max_upload_bytes: int = Field(default=50 * 1024 * 1024, gt=0)
    ingest_max_concurrent_uploads: int = Field(default=4, gt=0)
    tax_master_xlsx_max_zip_members: int = Field(default=128, gt=0)
    tax_master_xlsx_max_total_uncompressed_bytes: int = Field(
        default=64 * 1024 * 1024,
        gt=0,
    )
    tax_master_xlsx_max_member_uncompressed_bytes: int = Field(
        default=32 * 1024 * 1024,
        gt=0,
    )
    tax_master_xlsx_max_compression_ratio: int = Field(default=200, gt=0)
    tax_master_xlsx_max_worksheet_rows: int = Field(default=20_000, gt=0)
    tax_master_xlsx_max_worksheet_cells: int = Field(default=200_000, gt=0)

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
