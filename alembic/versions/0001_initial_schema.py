"""initial schema (Phase 1-A)

Revision ID: 0001
Revises:
Create Date: 2026-05-11

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "master_skus",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("sku_code", sa.String(64), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("jan_code", sa.String(32), nullable=True),
        sa.Column("attributes", JSONB, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("sku_code", name="uq_master_sku_code"),
    )
    op.create_index("ix_master_skus_sku_code", "master_skus", ["sku_code"])
    op.create_index("ix_master_skus_jan_code", "master_skus", ["jan_code"])

    op.create_table(
        "channel_sku_mappings",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "master_sku_id",
            sa.BigInteger,
            sa.ForeignKey("master_skus.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("channel_sku", sa.String(128), nullable=False),
        sa.Column("channel_product_id", sa.String(128), nullable=True),
        sa.Column("marketplace_id", sa.String(32), nullable=True),
        sa.Column("fulfillment_type", sa.String(16), nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("channel", "channel_sku", "marketplace_id", name="uq_channel_sku_mapping"),
    )
    op.create_index("ix_channel_sku_mappings_master_sku_id", "channel_sku_mappings", ["master_sku_id"])
    op.create_index("ix_channel_sku_mappings_channel", "channel_sku_mappings", ["channel"])

    op.create_table(
        "orders",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("channel_order_id", sa.String(128), nullable=False),
        sa.Column("marketplace_id", sa.String(32), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("ordered_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_payload", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("channel", "channel_order_id", name="uq_order_channel_orderid"),
    )
    op.create_index("ix_orders_channel", "orders", ["channel"])
    op.create_index("ix_orders_status", "orders", ["status"])
    op.create_index("ix_orders_ordered_at", "orders", ["ordered_at"])

    op.create_table(
        "order_items",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "order_id",
            sa.BigInteger,
            sa.ForeignKey("orders.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("line_id", sa.String(64), nullable=False),
        sa.Column("channel_sku", sa.String(128), nullable=False),
        sa.Column(
            "master_sku_id",
            sa.BigInteger,
            sa.ForeignKey("master_skus.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("quantity", sa.Integer, nullable=False),
        sa.Column("unit_price", sa.Numeric(12, 2), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False, server_default="JPY"),
        sa.Column("fulfillment_type", sa.String(16), nullable=True),
        sa.UniqueConstraint("order_id", "line_id", name="uq_order_item_line"),
        sa.CheckConstraint("quantity > 0", name="ck_order_items_qty_positive"),
    )
    op.create_index("ix_order_items_order_id", "order_items", ["order_id"])
    op.create_index("ix_order_items_master_sku", "order_items", ["master_sku_id"])

    op.create_table(
        "inventory_events",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "master_sku_id",
            sa.BigInteger,
            sa.ForeignKey("master_skus.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("quantity_delta", sa.Integer, nullable=False),
        sa.Column("source_channel", sa.String(32), nullable=True),
        sa.Column("source_order_id", sa.String(128), nullable=True),
        sa.Column("source_line_id", sa.String(64), nullable=True),
        sa.Column("reason", sa.String(255), nullable=True),
        sa.Column("operator", sa.String(128), nullable=True),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "event_type",
            "source_channel",
            "source_order_id",
            "source_line_id",
            name="uq_inventory_event_source",
        ),
    )
    op.create_index(
        "ix_inventory_events_sku_time",
        "inventory_events",
        ["master_sku_id", "occurred_at"],
    )

    op.create_table(
        "inventory_snapshots",
        sa.Column(
            "master_sku_id",
            sa.BigInteger,
            sa.ForeignKey("master_skus.id", ondelete="RESTRICT"),
            primary_key=True,
        ),
        sa.Column("on_hand_qty", sa.Integer, nullable=False, server_default="0"),
        sa.Column(
            "last_event_id",
            sa.BigInteger,
            sa.ForeignKey("inventory_events.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "mapping_alerts",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("channel_sku", sa.String(128), nullable=False),
        sa.Column("channel_product_id", sa.String(128), nullable=True),
        sa.Column("marketplace_id", sa.String(32), nullable=True),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("occurrence_count", sa.Integer, nullable=False, server_default="1"),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column(
            "resolved_master_sku_id",
            sa.BigInteger,
            sa.ForeignKey("master_skus.id", ondelete="RESTRICT"),
            nullable=True,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint(
            "channel", "channel_sku", "marketplace_id", name="uq_mapping_alert_target"
        ),
    )
    op.create_index("ix_mapping_alerts_channel", "mapping_alerts", ["channel"])
    op.create_index("ix_mapping_alerts_status", "mapping_alerts", ["status"])

    op.create_table(
        "webhook_logs",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("channel", sa.String(32), nullable=False),
        sa.Column("webhook_id", sa.String(128), nullable=False),
        sa.Column("topic", sa.String(64), nullable=False),
        sa.Column("hmac_valid", sa.Boolean, nullable=False),
        sa.Column("payload", JSONB, nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="received"),
        sa.UniqueConstraint("channel", "webhook_id", name="uq_webhook_log_id"),
    )
    op.create_index("ix_webhook_logs_channel", "webhook_logs", ["channel"])
    op.create_index("ix_webhook_logs_status", "webhook_logs", ["status"])


def downgrade() -> None:
    op.drop_table("webhook_logs")
    op.drop_table("mapping_alerts")
    op.drop_table("inventory_snapshots")
    op.drop_table("inventory_events")
    op.drop_table("order_items")
    op.drop_table("orders")
    op.drop_table("channel_sku_mappings")
    op.drop_table("master_skus")
