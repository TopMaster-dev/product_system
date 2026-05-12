"""InventoryEvent / InventorySnapshot — event-sourced inventory state.

Idempotency strategy:
- `inventory_events` has UNIQUE on (event_type, source_channel, source_order_id, source_line_id).
  For order-driven events all three source columns are populated, so the same
  order line cannot decrement stock twice.
- For events without a source order (manual_adjust / stocktake / receipt) the
  source_* columns are NULL; Postgres treats each NULL as distinct, so multiple
  manual adjustments coexist freely.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class InventoryEvent(Base):
    __tablename__ = "inventory_events"
    __table_args__ = (
        UniqueConstraint(
            "event_type",
            "source_channel",
            "source_order_id",
            "source_line_id",
            name="uq_inventory_event_source",
        ),
        Index("ix_inventory_events_sku_time", "master_sku_id", "occurred_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    master_sku_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("master_skus.id", ondelete="RESTRICT"),
        nullable=False,
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity_delta: Mapped[int] = mapped_column(Integer, nullable=False)
    source_channel: Mapped[str | None] = mapped_column(String(32), nullable=True)
    source_order_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_line_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    operator: Mapped[str | None] = mapped_column(String(128), nullable=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )


class InventorySnapshot(Base, TimestampMixin):
    """Materialized current stock per master_sku. Truth source is `inventory_events`."""

    __tablename__ = "inventory_snapshots"

    master_sku_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("master_skus.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    on_hand_qty: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_event_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("inventory_events.id", ondelete="SET NULL"),
        nullable=True,
    )
