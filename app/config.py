"""Application configuration (pydantic-settings)."""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

AppEnv = Literal["local", "dev", "staging", "prod"]
TaskQueueBackend = Literal["in_memory", "cloud_tasks"]
# Phase 1-B: which Rakuten RMS endpoint we use for pushing inventory.
# Decision pending in kickoff meeting; default to the more targeted endpoint.
RakutenInventoryApi = Literal["updateInventory", "updateItem"]


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

    # Phase 1-B: inventory push & reconcile
    # Default Shopify Location ID for push_inventory. Empty = auto-discover the
    # shop's single primary location at startup. Set explicitly only when the
    # shop has multiple locations.
    shopify_location_id: str = ""
    rakuten_inventory_api: RakutenInventoryApi = "updateInventory"

    # Slack notifications. Levels: critical / error / info. Empty URL disables
    # notifications (used in local dev and tests).
    slack_webhook_url: str = ""
    slack_notify_min_level: Literal["critical", "error", "info"] = "error"

    # Best-seller definition for the inventory list view. Top-N percent by
    # order_consumed count over the trailing window.
    best_seller_window_days: int = 30
    best_seller_top_percent: int = 20

    @property
    def is_production(self) -> bool:
        return self.app_env == "prod"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
