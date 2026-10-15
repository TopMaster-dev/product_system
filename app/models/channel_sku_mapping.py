"""ChannelSkuMapping — links a channel's SKU/product to a MasterSku."""

from __future__ import annotations

from sqlalchemy import BigInteger, Boolean, CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ChannelSkuMapping(Base, TimestampMixin):
    """One channel's sellable unit, mapped to one of our masters.

    `channel_sku` does not have the same GRAIN on every channel: on Shopify it is
    already the variant SKU, while on Rakuten it is 商品管理番号 — a PAGE, which for
    this shop always carries several SKUs (the catalogue is 100% 項目選択肢別在庫).
    `channel_variant_key` makes the grain explicit instead of implied:

        NULL      `channel_sku` is already the finest unit the channel sells.
                  Shopify, Amazon and wholesale rows stay NULL forever.
        set       the unit inside `channel_sku` — Rakuten's SKU管理番号.

    Both forms coexist for one page on purpose, because a page keeps its
    page-level row (which is what resolves its live order lines) while its
    variants are mapped one at a time. Resolve through
    `app.services.mapping.resolve_master_sku_id`, never with an ad-hoc query:
    a lookup that does not pin `channel_variant_key` matches the page row AND
    every variant row, and `scalar_one_or_none()` then raises.
    """

    __tablename__ = "channel_sku_mappings"
    __table_args__ = (
        # NULLS NOT DISTINCT is load-bearing: every existing row has a NULL
        # channel_variant_key, and NULLs comparing equal is what makes this
        # four-column constraint exactly as strict as the three-column one it
        # replaced. Under the default (NULLS DISTINCT) the same widening would
        # silently permit unlimited duplicate (channel, channel_sku) mappings.
        UniqueConstraint(
            "channel",
            "channel_sku",
            "marketplace_id",
            "channel_variant_key",
            name="uq_channel_sku_mapping",
            postgresql_nulls_not_distinct=True,
        ),
        # "" and NULL would be two spellings of "no variant", and only one of
        # them resolves.
        CheckConstraint(
            "channel_variant_key IS NULL OR length(channel_variant_key) > 0",
            name="ck_channel_sku_mapping_variant_key_not_blank",
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
    channel_variant_key: Mapped[str | None] = mapped_column(String(128), nullable=True)
    channel_product_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    marketplace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    fulfillment_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
