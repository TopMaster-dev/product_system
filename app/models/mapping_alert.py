"""MappingAlert — unmapped channel SKUs detected during ingestion."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class MappingAlert(Base, TimestampMixin):
    __tablename__ = "mapping_alerts"
    __table_args__ = (
        UniqueConstraint(
            "channel",
            "channel_sku",
            "marketplace_id",
            name="uq_mapping_alert_target",
            postgresql_nulls_not_distinct=True,
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    channel_sku: Mapped[str] = mapped_column(String(128), nullable=False)
    channel_product_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    marketplace_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="open", index=True)
    # Operator who took the alert into 対応中 (in_progress). Nullable.
    assignee: Mapped[str | None] = mapped_column(String(128), nullable=True)
    resolved_master_sku_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("master_skus.id", ondelete="RESTRICT"),
        nullable=True,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
