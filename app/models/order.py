"""Order / OrderItem — normalized orders ingested from channels."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Order(Base, TimestampMixin):
    __tablename__ = "orders"
    __table_args__ = (
        UniqueConstraint("channel", "channel_order_id", name="uq_order_channel_orderid"),
        Index("ix_orders_ordered_at", "ordered_at"),
        # Phase 2 (migration 0009): channel-scoped period aggregation for the
        # sales dashboards; ix_orders_ordered_at alone cannot filter by channel.
        Index("ix_orders_channel_ordered_at", "channel", "ordered_at"),
        # Phase 2 (migration 0014): the hourly rollup asks "which orders were
        # touched since the last run?". ix_orders_channel_ordered_at cannot
        # serve it — it leads with `channel`, and a btree cannot seek on a range
        # over a non-leading column. Without this the job read all 10,859 rows
        # every hour (EXPLAIN, 2026-09-23).
        Index("ix_orders_updated_at", "updated_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    channel_order_id: Mapped[str] = mapped_column(String(128), nullable=False)
    marketplace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    ordered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)


class OrderItem(Base):
    __tablename__ = "order_items"
    __table_args__ = (
        UniqueConstraint("order_id", "line_id", name="uq_order_item_line"),
        Index("ix_order_items_master_sku", "master_sku_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    order_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("orders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    line_id: Mapped[str] = mapped_column(String(64), nullable=False)
    channel_sku: Mapped[str] = mapped_column(String(128), nullable=False)
    master_sku_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("master_skus.id", ondelete="RESTRICT"),
        nullable=True,
    )
    quantity: Mapped[int] = mapped_column(nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="JPY")
    fulfillment_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
