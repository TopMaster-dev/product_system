"""sku_velocity — materialised sales velocity and the dynamic low-stock threshold

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-18

Phase 2 W8, P2-015 / P2-016 / P2-017.

The low-stock threshold stops being the fixed 10 in settings and becomes a
per-SKU value derived from how fast that SKU actually sells. `stock_status.py`
was written in W1 expecting exactly this — every function there already takes
the threshold as an argument rather than reading a constant, with a note saying
W6 would make it per-SKU.

WHY A TABLE AND NOT A SQL EXPRESSION

The inventory screen filters, sorts and COUNTS by stock status. The count badges
and the row list are separate queries, and they must agree — that is the defect
W1's consolidation existed to prevent. So the threshold has to be available to
SQL, which leaves two options: express the formula in SQL, or materialise it.

Expressing it in SQL means the same clamped, floored arithmetic exists in both
Python (for the risk screen) and SQL (for the inventory screen), and the two
would diverge the first time either was adjusted. This project has been bitten
by duplicated definitions repeatedly — the threshold literal in twelve places,
two implementations of "is this SKU still in use". Materialising keeps ONE
definition, in `app/services/velocity.py`, which is unit-tested; SQL only ever
reads the result.

The freshness cost is real and bounded: values are as current as the last
rollup, which runs hourly.

ALLOCATED IN docs/24 AS `sku_velocity` + `notification_logs`. Only the first
table is created here. `notification_logs` belongs to P2-019 Slack 欠品通知,
which docs/23 moved to Phase 2.5; creating it now would add a table nothing
reads, and an empty table with no writer reads as a broken feature.

NO INDEX BEYOND THE PRIMARY KEY. One row per master SKU — about 1,100 — and
every query joins it on that key. A secondary index would cost a write per
rollup to serve a scan that is already fast.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "sku_velocity",
        # The master IS the key. One current velocity per SKU; history lives in
        # sku_daily_stock, which this is derived from.
        sa.Column(
            "master_sku_id",
            sa.BigInteger(),
            sa.ForeignKey("master_skus.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        # The window the rate was measured over, and how much of it actually had
        # data. days_observed is the DENOMINATOR — a SKU registered last week has
        # 7 days inside a 28-day window, and dividing by 28 would report a
        # quarter of its real rate.
        sa.Column("window_days", sa.Integer(), nullable=False),
        sa.Column("days_observed", sa.Integer(), nullable=False),
        sa.Column("consumed_qty", sa.Integer(), nullable=False, server_default="0"),
        # Numeric, not float: this is read back into a Decimal comparison and
        # displayed to two places.
        sa.Column("per_day", sa.Numeric(10, 4), nullable=False, server_default="0"),
        # The clamped, rounded threshold. Stored rather than recomputed in SQL
        # so the inventory screen's badges, list and sort all read one number.
        sa.Column("low_stock_threshold", sa.Integer(), nullable=False),
        # Which of GOOD / LIMITED / INSUFFICIENT. Carried so a screen can show
        # it without re-deriving the rule, and so a stale row is legible.
        sa.Column("confidence", sa.String(16), nullable=False),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("sku_velocity")
