"""add sync_attempts table

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-14

Phase 1-B feature F1.1.
Records per-attempt history of channel inventory push and reconcile runs.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0003"
down_revision: str | Sequence[str] | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "sync_attempts",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("attempt_type", sa.String(32), nullable=False),
        sa.Column("channel", sa.String(32), nullable=True),
        sa.Column(
            "master_sku_id",
            sa.BigInteger,
            sa.ForeignKey("master_skus.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("payload", JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text, nullable=True),
        sa.Column("response_payload", JSONB, nullable=True),
        sa.Column("attempt_count", sa.Integer, nullable=False, server_default="1"),
        sa.Column(
            "parent_attempt_id",
            sa.BigInteger,
            sa.ForeignKey("sync_attempts.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
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
    op.create_index(
        "ix_sync_attempts_attempt_type",
        "sync_attempts",
        ["attempt_type"],
    )
    op.create_index("ix_sync_attempts_channel", "sync_attempts", ["channel"])
    op.create_index("ix_sync_attempts_status", "sync_attempts", ["status"])
    op.create_index(
        "ix_sync_attempts_status_started",
        "sync_attempts",
        ["status", "started_at"],
    )
    op.create_index(
        "ix_sync_attempts_master_sku_started",
        "sync_attempts",
        ["master_sku_id", "started_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_sync_attempts_master_sku_started", table_name="sync_attempts")
    op.drop_index("ix_sync_attempts_status_started", table_name="sync_attempts")
    op.drop_index("ix_sync_attempts_status", table_name="sync_attempts")
    op.drop_index("ix_sync_attempts_channel", table_name="sync_attempts")
    op.drop_index("ix_sync_attempts_attempt_type", table_name="sync_attempts")
    op.drop_table("sync_attempts")
