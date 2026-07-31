"""MasterSku — the canonical, channel-agnostic SKU."""

from __future__ import annotations

from typing import Any

from sqlalchemy import BigInteger, Boolean, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class MasterSku(Base, TimestampMixin):
    __tablename__ = "master_skus"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    sku_code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    jan_code: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # Shopify product/variant image, synced by app.cli.sync_shopify_images so the
    # admin screens can show a thumbnail. Nullable — not every SKU has an image.
    image_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    attributes: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    # A bundle/set or shared-stock (anklet/bracelet) parent — availability is
    # derived from its bundle_components, never stored, and never pushed directly.
    is_bundle: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false"), default=False
    )
