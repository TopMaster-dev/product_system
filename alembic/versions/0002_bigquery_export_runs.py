"""add bigquery_export_runs table

Revision ID: 0002
Revises: 0001
Create Date: 2026-05-11

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | Sequence[str] | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "bigquery_export_runs",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("table_name", sa.String(64), nullable=False),
        sa.Column("mode", sa.String(16), nullable=False),
        sa.Column("since", sa.DateTime(timezone=True), nullable=True),
        sa.Column("until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("row_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error", sa.Text, nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("table_name", "until", name="uq_bq_export_table_until"),
    )
    op.create_index("ix_bq_export_runs_table_name", "bigquery_export_runs", ["table_name"])
    op.create_index("ix_bq_export_runs_status", "bigquery_export_runs", ["status"])


def downgrade() -> None:
    op.drop_table("bigquery_export_runs")
