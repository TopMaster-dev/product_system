"""ix_orders_updated_at — the hourly rollup stops scanning the whole orders table

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-23

Phase 2 W10, P2-043.

`AnalyticsRollupService.dates_to_rebuild` decides which JST days to rebuild by
asking two questions since the last successful run:

    inventory_events.created_at > since   -> ix_inventory_events_created_at (0009)
    orders.updated_at        > since   -> nothing

The first was indexed in 0009; the second was not, and nothing made that
visible. EXPLAIN against production on 2026-09-23, with 10,859 orders:

    Seq Scan on orders  (cost=0.00..925.82 rows=1 width=4)

It reads every order in the table, once an hour, to find the handful touched in
the last hour — and the cost grows with the table for ever. At roughly 2,000
orders a month it is merely wasteful today and will not stay that way.

WHY updated_at ALONE

The predicate is a bare range on one column, so a single-column btree serves it
exactly. `ix_orders_channel_ordered_at` cannot: it leads with `channel`, and a
range on a non-leading column is not something a btree can seek on.

WHY NOT CONCURRENTLY

10,859 rows builds in milliseconds and the brief ACCESS EXCLUSIVE lock is
shorter than the hourly job's own transaction. CONCURRENTLY cannot run inside
alembic's transaction anyway, and the table is nowhere near the size that would
justify the extra machinery.

The permanent guard is `tests/integration/test_query_plans.py`, which fails if
any of these four scans loses its index again.
"""

from __future__ import annotations

from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index("ix_orders_updated_at", "orders", ["updated_at"])


def downgrade() -> None:
    op.drop_index("ix_orders_updated_at", table_name="orders")
