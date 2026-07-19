"""add bundle_components, master_skus.is_bundle, widen inventory-event key

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-20

Phase 1-B 組み合わせ商品 / 共有在庫 (bundle & shared-stock inventory).
- bundle_components: master<->master BOM (sets + anklet/bracelet shared stock).
- master_skus.is_bundle: discriminator for derived-availability parents (excluded
  from push & reconcile).
- widen uq_inventory_event_source to include master_sku_id, so one order line can
  fan out to per-component decrements without colliding on the idempotency key.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "master_skus",
        sa.Column(
            "is_bundle",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )

    op.create_table(
        "bundle_components",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column(
            "bundle_master_sku_id",
            sa.BigInteger,
            sa.ForeignKey("master_skus.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "component_master_sku_id",
            sa.BigInteger,
            sa.ForeignKey("master_skus.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("quantity_per", sa.Integer, nullable=False, server_default="1"),
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
            "bundle_master_sku_id",
            "component_master_sku_id",
            name="uq_bundle_component",
        ),
    )
    op.create_index(
        "ix_bundle_components_component",
        "bundle_components",
        ["component_master_sku_id"],
    )

    # Widen the inventory-event idempotency key so per-component fan-out from one
    # bundle order line does not collide (each component is a distinct master_sku_id).
    op.drop_constraint("uq_inventory_event_source", "inventory_events", type_="unique")
    op.create_unique_constraint(
        "uq_inventory_event_source",
        "inventory_events",
        ["event_type", "source_channel", "source_order_id", "source_line_id", "master_sku_id"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_inventory_event_source", "inventory_events", type_="unique")
    op.create_unique_constraint(
        "uq_inventory_event_source",
        "inventory_events",
        ["event_type", "source_channel", "source_order_id", "source_line_id"],
    )
    op.drop_index("ix_bundle_components_component", table_name="bundle_components")
    op.drop_table("bundle_components")
    op.drop_column("master_skus", "is_bundle")
