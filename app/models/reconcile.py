"""ReconcileRun + ReconcileDiff — CROSS MALL based daily reconciliation.

Phase 1-B F1.3.

A ReconcileRun is one execution of the reconciliation pipeline (typically
once per day, triggered by Cloud Scheduler). It collects ReconcileDiff
rows — one per SKU where the central DB disagrees with the CROSS MALL
inventory CSV. Each diff carries a decision (`pending`/`approved`/`skipped`);
approving a diff creates a `stocktake` inventory event and links to it via
applied_event_id. The run transitions through running → pending_approval
→ applied (or cancelled) as the operator works through the diff list.
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
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ReconcileRun(Base, TimestampMixin):
    __tablename__ = "reconcile_runs"
    __table_args__ = (
        Index("ix_reconcile_runs_status_started", "status", "started_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    csv_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    diff_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    applied_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    triggered_by: Mapped[str] = mapped_column(String(128), nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
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
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class ReconcileDiff(Base, TimestampMixin):
    __tablename__ = "reconcile_diffs"
    __table_args__ = (
        UniqueConstraint(
            "reconcile_run_id",
            "master_sku_id",
            name="uq_reconcile_diff_run_sku",
        ),
        Index(
            "ix_reconcile_diffs_run_decision",
            "reconcile_run_id",
            "decision",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    reconcile_run_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("reconcile_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    master_sku_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("master_skus.id", ondelete="RESTRICT"),
        nullable=False,
    )
    current_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    target_qty: Mapped[int] = mapped_column(Integer, nullable=False)
    delta: Mapped[int] = mapped_column(Integer, nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    applied_event_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("inventory_events.id", ondelete="SET NULL"),
        nullable=True,
    )
    decided_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )
