"""Application configuration (pydantic-settings)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

AppEnv = Literal["local", "dev", "staging", "prod"]
TaskQueueBackend = Literal["in_memory", "cloud_tasks"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    app_env: AppEnv = "local"
    app_log_level: str = "INFO"
    app_timezone: str = "Asia/Tokyo"

    database_url: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/product_system",
    )
    database_url_sync: str = Field(
        default="postgresql+psycopg2://postgres:postgres@localhost:5432/product_system",
    )

    task_queue_backend: TaskQueueBackend = "in_memory"

    # Rakuten (filled later with client credentials)
    rakuten_service_secret: str = ""
    rakuten_license_key: str = ""
    rakuten_shop_url: str = ""

    # Shopify (filled later with client credentials)
    shopify_shop_domain: str = ""
    shopify_access_token: str = ""
    shopify_webhook_secret: str = ""
    shopify_api_version: str = "2025-04"

    # GCP (filled later)
    gcp_project_id: str = ""
    gcp_region: str = "asia-northeast1"
    bigquery_dataset: str = ""

    # Cloud Tasks (only used when task_queue_backend == "cloud_tasks")
    cloud_tasks_queue: str = ""
    cloud_tasks_target_url: str = ""
    cloud_tasks_invoker_sa: str = ""

    # Admin UI
    admin_username: str = "admin"
    admin_password: str = "change_me_in_production"  # noqa: S105

    @property
    def is_production(self) -> bool:
        return self.app_env == "prod"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
