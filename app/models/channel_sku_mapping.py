"""ChannelSkuMapping — links a channel's SKU/product to a MasterSku."""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ChannelSkuMapping(Base, TimestampMixin):
    __tablename__ = "channel_sku_mappings"
    __table_args__ = (
        UniqueConstraint(
            "channel",
            "channel_sku",
            "marketplace_id",
            name="uq_channel_sku_mapping",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    master_sku_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("master_skus.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    channel_sku: Mapped[str] = mapped_column(String(128), nullable=False)
    channel_product_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    marketplace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    fulfillment_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
