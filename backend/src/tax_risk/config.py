from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Environment-backed application settings."""

    database_url: str = "postgresql+psycopg://tax_risk:tax_risk@localhost:5432/tax_risk"
    redis_url: str = "redis://localhost:6379/0"
    environment: Literal["development", "test", "production"] = "development"
    development_principal_enabled: bool = False

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
