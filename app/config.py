from functools import lru_cache
from datetime import date, datetime
from uuid import UUID

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="MI_", extra="ignore")
    database_url: SecretStr
    worker_database_url: SecretStr | None = None
    jwt_secret: SecretStr | None = None
    jwt_issuer: str = "marketing-intelligence"
    jwt_audience: str = "marketing-intelligence-api"
    access_minutes: int = Field(default=10, ge=1, le=15)
    refresh_days: int = Field(default=7, ge=1, le=30)
    allow_signup: bool = False
    bootstrap_email: str | None = None
    bootstrap_token_sha256: SecretStr | None = None
    bootstrap_expires_at: datetime | None = None
    bootstrap_organization: str = "NOETIS"
    service_user_id: str | None = None
    crawl_worker_database_url: SecretStr | None = None
    crawl_service_user_id: UUID | None = None
    crawl_brand_id: UUID | None = None
    crawl_scheduler_enabled: bool = False
    ui_directory: str | None = None
    model_provider: str = "disabled"
    allow_model_calls: bool = False
    allow_planning_calls: bool = False
    planning_max_input_tokens: int = Field(default=24000, ge=1000, le=32000)
    planning_max_output_tokens: int = Field(default=8192, ge=1024, le=16384)
    model_max_cost_paise: int | None = Field(default=None, ge=1)
    anthropic_api_key: SecretStr | None = None
    anthropic_model: str | None = None
    anthropic_version: str = "2026-10-21"
    brave_api_key: SecretStr | None = None
    allow_search_calls: bool = False
    research_max_input_tokens: int = Field(default=16000, ge=1000, le=32000)
    research_max_output_tokens: int = Field(default=4096, ge=512, le=8192)
    model_input_paise_per_million: int | None = Field(default=None, ge=0)
    model_output_paise_per_million: int | None = Field(default=None, ge=0)
    pricing_as_of: str | None = None
    research_max_cost_paise: int | None = Field(default=None, ge=1)
    research_pilot_enabled: bool = False
    research_pilot_brand_id: UUID | None = None
    research_pilot_service_user_id: UUID | None = None
    research_pilot_worker_database_url: SecretStr | None = None

    @model_validator(mode="after")
    def secure_secret(self):
        if self.jwt_secret is not None and (len(self.jwt_secret.get_secret_value()) < 32 or self.jwt_secret.get_secret_value().startswith("REPLACE_")):
            raise ValueError("MI_JWT_SECRET must contain at least 32 characters")
        if self.pricing_as_of:
            date.fromisoformat(self.pricing_as_of)
        return self

    def signing_secret(self) -> str:
        if self.jwt_secret is None:
            raise RuntimeError("MI_JWT_SECRET is required by the API")
        return self.jwt_secret.get_secret_value()


@lru_cache
def settings() -> Settings:
    return Settings()
