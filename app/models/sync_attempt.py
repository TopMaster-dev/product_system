"""SyncAttempt — per-attempt log of inventory push and reconcile operations.

Records both successes and failures. The admin UI's `/admin/sync-errors`
page displays only `status='failed'` rows; the success records exist so the
operator can audit "did we actually try to push this SKU?" and so that
follow-up retries can be linked via `parent_attempt_id`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class SyncAttempt(Base, TimestampMixin):
    __tablename__ = "sync_attempts"
    __table_args__ = (
        Index("ix_sync_attempts_status_started", "status", "started_at"),
        Index("ix_sync_attempts_master_sku_started", "master_sku_id", "started_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    attempt_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    channel: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    master_sku_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("master_skus.id", ondelete="RESTRICT"),
        nullable=True,
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    response_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    parent_attempt_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("sync_attempts.id", ondelete="SET NULL"),
        nullable=True,
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
