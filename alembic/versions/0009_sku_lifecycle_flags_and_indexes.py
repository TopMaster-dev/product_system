"""master_skus lifecycle flags + all Phase 2 hot-path indexes

Revision ID: 0009
Revises: 0008
Create Date: 2026-08-19

Phase 2 W1 foundation. Two unrelated-looking things ship together on purpose:

1. `master_skus` lifecycle columns
   - is_stock_managed / non_inventory_kind: gift boxes, coupons and made-to-order
     items "sell" with every order but hold no stock, so they slid to -12509 in
     Phase 1-B and polluted the best-seller ranking. A CHECK keeps the flag and
     the kind in lockstep so neither can be set alone.
   - archived_at / archived_reason: retire the ~400 legacy product-level masters
     left at 0 by the variant cutover WITHOUT deleting them, so historical sales
     still aggregate (archive is a visibility concept, not an inventory one).

2. EVERY hot-path index for Phase 2, created HERE AND ONLY HERE.
   Per docs/24, no other migration may add an index on inventory_events / orders.
   Two independently-written Phase 2 designs both created
   `ix_inventory_events_consumed_time`; whichever ran second would have failed
   `alembic upgrade head` in production while CI stayed green (conftest builds the
   schema with Base.metadata.create_all and never runs alembic).

   These are not only Phase 2 features: `best_seller_ids()` already runs an
   occurred_at-only aggregation on every /admin/inventory request, and
   `ix_inventory_events_sku_time` leads with master_sku_id so it cannot serve it
   (PG15 has no index skip scan). This is also a Phase 1-B performance fix.

All columns are nullable or defaulted, so the previous image keeps running
against this schema (migrate first, then deploy).
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Alembic runs this whole function in ONE transaction, so the ACCESS
    # EXCLUSIVE lock taken by the first ALTER TABLE is held until COMMIT — i.e.
    # across every CREATE INDEX below. On prod (db-f1-micro, Rakuten polling
    # every 5 minutes) that would stall order ingestion, and without a timeout a
    # blocked ALTER also queues behind readers and blocks everything behind it.
    # Fail fast instead: if the locks are not free within 5s, abort cleanly and
    # retry during a quiet window rather than degrade the live system.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")

    # --- 1. master_skus lifecycle flags ------------------------------------
    op.add_column(
        "master_skus",
        sa.Column(
            "is_stock_managed",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
    )
    op.add_column("master_skus", sa.Column("non_inventory_kind", sa.String(length=32), nullable=True))
    op.add_column(
        "master_skus",
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column("master_skus", sa.Column("archived_reason", sa.String(length=255), nullable=True))
    op.create_check_constraint(
        "ck_master_sku_non_inventory_kind",
        "master_skus",
        "is_stock_managed = (non_inventory_kind IS NULL)",
    )

    # --- 1b. reconcile_diffs.applied_delta ---------------------------------
    # approve_diff recomputes the delta under the snapshot lock, so the stored
    # scan-time `delta` no longer describes what was written. Persist the applied
    # value so the detail screen and the audit CSV can show the truth.
    op.add_column("reconcile_diffs", sa.Column("applied_delta", sa.Integer(), nullable=True))

    # --- 2. hot-path indexes (this migration only) -------------------------
    op.create_index(
        "ix_inventory_events_consumed_time",
        "inventory_events",
        ["occurred_at"],
        postgresql_include=["master_sku_id", "quantity_delta"],
        postgresql_where=sa.text("event_type = 'order_consumed'"),
    )
    op.create_index(
        "ix_inventory_events_returned_time",
        "inventory_events",
        ["occurred_at"],
        postgresql_include=["master_sku_id", "quantity_delta"],
        postgresql_where=sa.text("event_type = 'cancellation_returned'"),
    )
    op.create_index("ix_inventory_events_created_at", "inventory_events", ["created_at"])
    op.create_index("ix_orders_channel_ordered_at", "orders", ["channel", "ordered_at"])


def downgrade() -> None:
    op.drop_index("ix_orders_channel_ordered_at", table_name="orders")
    op.drop_index("ix_inventory_events_created_at", table_name="inventory_events")
    op.drop_index("ix_inventory_events_returned_time", table_name="inventory_events")
    op.drop_index("ix_inventory_events_consumed_time", table_name="inventory_events")
    op.drop_column("reconcile_diffs", "applied_delta")
    op.drop_constraint("ck_master_sku_non_inventory_kind", "master_skus", type_="check")
    op.drop_column("master_skus", "archived_reason")
    op.drop_column("master_skus", "archived_at")
    op.drop_column("master_skus", "non_inventory_kind")
    op.drop_column("master_skus", "is_stock_managed")
