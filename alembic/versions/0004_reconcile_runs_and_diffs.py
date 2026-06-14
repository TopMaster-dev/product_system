"""add reconcile_runs and reconcile_diffs tables

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-14

Phase 1-B feature F1.3.
Persists CROSS MALL based reconciliation runs and per-SKU diffs awaiting
operator decision (approve / skip).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "reconcile_runs",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("csv_filename", sa.String(255), nullable=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("diff_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("applied_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("triggered_by", sa.String(128), nullable=False),
        sa.Column("approved_by", sa.String(128), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )
    op.create_index("ix_reconcile_runs_status", "reconcile_runs", ["status"])
    op.create_index(
        "ix_reconcile_runs_status_started",
        "reconcile_runs",
        ["status", "started_at"],
    )

    op.create_table(
        "reconcile_diffs",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "reconcile_run_id",
            sa.BigInteger,
            sa.ForeignKey("reconcile_runs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "master_sku_id",
            sa.BigInteger,
            sa.ForeignKey("master_skus.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("current_qty", sa.Integer, nullable=False),
        sa.Column("target_qty", sa.Integer, nullable=False),
        sa.Column("delta", sa.Integer, nullable=False),
        sa.Column(
            "decision",
            sa.String(16),
            nullable=False,
            server_default="pending",
        ),
        sa.Column(
            "applied_event_id",
            sa.BigInteger,
            sa.ForeignKey("inventory_events.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("decided_by", sa.String(128), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "reconcile_run_id",
            "master_sku_id",
            name="uq_reconcile_diff_run_sku",
        ),
    )
    op.create_index(
        "ix_reconcile_diffs_reconcile_run_id",
        "reconcile_diffs",
        ["reconcile_run_id"],
    )
    op.create_index(
        "ix_reconcile_diffs_run_decision",
        "reconcile_diffs",
        ["reconcile_run_id", "decision"],
    )


def downgrade() -> None:
    op.drop_index("ix_reconcile_diffs_run_decision", table_name="reconcile_diffs")
    op.drop_index("ix_reconcile_diffs_reconcile_run_id", table_name="reconcile_diffs")
    op.drop_table("reconcile_diffs")
    op.drop_index("ix_reconcile_runs_status_started", table_name="reconcile_runs")
    op.drop_index("ix_reconcile_runs_status", table_name="reconcile_runs")
    op.drop_table("reconcile_runs")
