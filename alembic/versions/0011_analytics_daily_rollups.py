"""daily analytics rollups

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-03

Phase 2 W4. The aggregation layer every analytics screen reads.

Measured on this dataset: the fleet-wide 在庫推移 replayed from raw
`inventory_events` is ~1992ms and spills ~13MB to temp; from these rollups
172ms; from `daily_kpi_snapshots` 2.4ms. Production is db-f1-micro, shared
vCPU, also running Rakuten polling every five minutes — a dashboard seq-scan
starves order ingestion, so this is a correctness measure as much as a speed one.

`stat_date` is a JST calendar date in every table (see app/services/timeframe.py).
UTC dates would misfile every order placed 00:00-09:00 JST.

NO INDEX DDL HERE. Per docs/24, migration 0009 owns every hot-path index; two
migrations creating one name is how `alembic upgrade head` fails in production
while CI stays green (conftest builds the schema with create_all and never runs
alembic). The UNIQUE constraints below are not indexes-for-speed — they are the
correctness guarantee the rollup's DELETE+INSERT depends on, and they belong
with the tables they constrain.

Every table is new and empty, so REQUIRED/NOT NULL columns are safe here: this
is a create, not an alter.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | Sequence[str] | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # No ALTER on an existing hot table here, so no lock_timeout dance is
    # needed — CREATE TABLE takes no lock anything else is waiting on.
    op.create_table(
        "sku_daily_stock",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("stat_date", sa.Date(), nullable=False),
        sa.Column("master_sku_id", sa.BigInteger(), nullable=False),
        sa.Column("on_hand_qty", sa.Integer(), nullable=False),
        sa.Column("consumed_qty", sa.Integer(), server_default="0", nullable=False),
        sa.Column("returned_qty", sa.Integer(), server_default="0", nullable=False),
        # Reserved for 原価 (P2-014). Nullable and unused, so enabling stock
        # valuation later is a rebuild rather than an ALTER on a table that by
        # then holds a year of daily rows.
        sa.Column("unit_cost_jpy", sa.Numeric(12, 2), nullable=True),
        sa.Column("stock_value_jpy", sa.Numeric(14, 2), nullable=True),
        sa.ForeignKeyConstraint(["master_sku_id"], ["master_skus.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stat_date", "master_sku_id", name="uq_sku_daily_stock_day_sku"),
    )

    op.create_table(
        "sku_daily_sales",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("stat_date", sa.Date(), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("master_sku_id", sa.BigInteger(), nullable=False),
        # Denormalised: keeps the category a sale was MADE under, so a later
        # re-categorisation does not rewrite history.
        sa.Column("category_id", sa.BigInteger(), nullable=True),
        sa.Column("order_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("quantity", sa.Integer(), server_default="0", nullable=False),
        sa.Column("gross_sales_jpy", sa.Numeric(14, 2), server_default="0", nullable=False),
        sa.Column("cancelled_quantity", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cancelled_sales_jpy", sa.Numeric(14, 2), server_default="0", nullable=False),
        sa.ForeignKeyConstraint(["master_sku_id"], ["master_skus.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["category_id"], ["product_categories.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "stat_date", "channel", "master_sku_id", name="uq_sku_daily_sales_day_channel_sku"
        ),
    )

    # Order lines that never resolved to a master. Every other table here is
    # keyed by master_sku_id, so without this they would simply vanish and the
    # analytics would disagree with real revenue by an invisible amount.
    op.create_table(
        "daily_unmapped_sales",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("stat_date", sa.Date(), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("channel_sku", sa.String(length=128), nullable=False),
        sa.Column("order_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("quantity", sa.Integer(), server_default="0", nullable=False),
        sa.Column("gross_sales_jpy", sa.Numeric(14, 2), server_default="0", nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "stat_date", "channel", "channel_sku", name="uq_daily_unmapped_day_channel_sku"
        ),
    )

    op.create_table(
        "daily_kpi_snapshots",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("stat_date", sa.Date(), nullable=False),
        sa.Column("total_on_hand_qty", sa.Integer(), server_default="0", nullable=False),
        sa.Column("sku_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("out_of_stock_sku_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("negative_stock_sku_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("order_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("sold_quantity", sa.Integer(), server_default="0", nullable=False),
        sa.Column("gross_sales_jpy", sa.Numeric(14, 2), server_default="0", nullable=False),
        sa.Column("unmapped_sales_jpy", sa.Numeric(14, 2), server_default="0", nullable=False),
        sa.Column("snapshot_drift_count", sa.Integer(), nullable=True),
        sa.Column("stock_value_jpy", sa.Numeric(16, 2), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("stat_date", name="uq_daily_kpi_date"),
    )

    # Deliberately NO unique on (job_name, stat_date): the hourly job rebuilds
    # recent days repeatedly by design, and uniqueness here would turn normal
    # operation into an IntegrityError. Uniqueness belongs on the data tables.
    op.create_table(
        "analytics_rollup_runs",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("job_name", sa.String(length=64), nullable=False),
        sa.Column("first_date", sa.Date(), nullable=True),
        sa.Column("last_date", sa.Date(), nullable=True),
        sa.Column("days_rebuilt", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("error", sa.String(length=2000), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("triggered_by", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("analytics_rollup_runs")
    op.drop_table("daily_kpi_snapshots")
    op.drop_table("daily_unmapped_sales")
    op.drop_table("sku_daily_sales")
    op.drop_table("sku_daily_stock")
