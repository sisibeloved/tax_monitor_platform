from typing import Literal, Self

from pydantic import Field, model_validator
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
    celery_task_always_eager: bool = False
    celery_task_eager_propagates: bool = False
    celery_task_store_eager_result: bool = False
    celery_visibility_timeout_seconds: int = Field(default=3_600, gt=0)
    celery_result_expires_seconds: int = Field(default=86_400, gt=0)
    quarterly_worker_concurrency: int = Field(default=4, gt=0)
    quarterly_task_soft_time_limit_seconds: int = Field(default=300, gt=0)
    quarterly_task_time_limit_seconds: int = Field(default=330, gt=0)
    quarterly_task_max_retries: int = Field(default=3, ge=0)
    quarterly_task_retry_backoff_seconds: int = Field(default=5, gt=0)

    @model_validator(mode="after")
    def validate_quarterly_worker_timeouts(self) -> Self:
        if self.quarterly_task_time_limit_seconds <= (
            self.quarterly_task_soft_time_limit_seconds
        ):
            raise ValueError("quarterly hard time limit must exceed its soft time limit")
        if self.celery_visibility_timeout_seconds <= self.quarterly_task_time_limit_seconds:
            raise ValueError("Celery visibility timeout must exceed the quarterly hard time limit")
        return self

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
