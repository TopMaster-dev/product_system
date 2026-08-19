"""BigQueryExportRun — tracks daily-export watermarks per source table."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class BigQueryExportRun(Base):
    __tablename__ = "bigquery_export_runs"
    __table_args__ = (
        UniqueConstraint("table_name", "until", name="uq_bq_export_table_until"),
        # Named explicitly to match what migration 0002 actually created. Using
        # `index=True` made SQLAlchemy auto-name these `ix_bigquery_export_runs_*`,
        # which no migration ever produced — harmless at runtime, but it is exactly
        # the ORM/migration drift scripts/check_migration_parity.py exists to catch.
        Index("ix_bq_export_runs_table_name", "table_name"),
        Index("ix_bq_export_runs_status", "status"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    table_name: Mapped[str] = mapped_column(String(64), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False)  # incremental | snapshot
    since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
