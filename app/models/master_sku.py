"""MasterSku — the canonical, channel-agnostic SKU."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, Boolean, CheckConstraint, DateTime, String, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class MasterSku(Base, TimestampMixin):
    __tablename__ = "master_skus"
    __table_args__ = (
        # 在庫管理対象外フラグと種別は常に対で設定される(片方だけの状態を作らせない)。
        CheckConstraint(
            "is_stock_managed = (non_inventory_kind IS NULL)",
            name="ck_master_sku_non_inventory_kind",
        ),
    )

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
    # --- Phase 2 lifecycle flags (migration 0009) ---------------------------
    # 在庫を持たない商品(ギフトボックス・クーポン・受注生産など)。false のとき
    # InventoryService.resolve_consumption() が [] を返し、消費もキャンセル補償も
    # 発生しない。片側だけ効かせるとキャンセルが幽霊在庫を生むため、両経路が
    # 同時に no-op になるこの一点で判定する。
    is_stock_managed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), default=True
    )
    non_inventory_kind: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # 変異体移行で役目を終えた旧マスタなどの retire 用。可視性の概念であり、
    # 在庫の概念ではない。現時点の画面は除外するが、期間集計は除外しない
    # (除外すると移行前の売上が消え前期間比較が壊れる)。
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    archived_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
