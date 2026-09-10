"""reconcile run_type discriminator + count metadata

Revision ID: 0012
Revises: 0011
Create Date: 2026-09-08

Phase 2 W5. Lets one approval path serve three kinds of run.

`reconcile_runs` currently holds only the daily CROSS MALL reconciliation. Two
more kinds are coming: the Shopify stock audit that REPLACES CROSS MALL when it
shuts down (P2-035), and the physical stocktake (P2-028). All three ask the same
question — "the system says X, reality says Y, apply the difference?" — and all
three want the same reviewed-and-approved path, which is the only code that
deliberately overwrites a snapshot. Duplicating it would mean maintaining the
riskiest write in the system in three places.

So: one discriminator column, four nullable metadata columns, and the existing
machinery unchanged.

`server_default='reconcile'` is load-bearing. Every existing row is a CROSS MALL
run, and without the default they would land NULL and vanish from the reconcile
screens the moment the filters go in.

REALLOCATED FROM 0013 (docs/24, 2026-09-08). This was scheduled for W7 alongside
the stocktake, but the Shopify audit needs the same discriminator and now runs
first. Alembic is linear — 0013 cannot be applied before 0012 — so the numbering
follows the sprint order rather than the original plan.

NO INDEX DDL, per docs/24 and the note in 0011. An index on (run_type,
started_at) was in the original design, but `reconcile_runs` holds roughly one
row per day; at a few hundred rows a sequential scan beats an index lookup, and
the list query is already capped at 100. Adding it now would cost a write on
every run to serve a scan that is faster without it.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

#: Kept in sync with app.models.reconcile.ReconcileRunTypeEnum.
_DEFAULT_RUN_TYPE = "reconcile"


def upgrade() -> None:
    op.add_column(
        "reconcile_runs",
        sa.Column(
            "run_type",
            sa.String(16),
            nullable=False,
            server_default=_DEFAULT_RUN_TYPE,
        ),
    )
    # Who counted, and as of when. Meaningful for a stocktake, and for a
    # Shopify audit they record which sweep produced the numbers.
    op.add_column("reconcile_runs", sa.Column("counted_by", sa.String(128), nullable=True))
    op.add_column("reconcile_runs", sa.Column("counted_on", sa.Date(), nullable=True))
    # What was counted — "2階の棚のみ" — so a partial count is not read as a
    # full one, which would turn every uncounted SKU into a false diff.
    op.add_column("reconcile_runs", sa.Column("scope_note", sa.Text(), nullable=True))
    op.add_column("reconcile_runs", sa.Column("counted_sku_count", sa.Integer(), nullable=True))
    # Per-line remark from whoever counted ("箱破損のため別置き").
    op.add_column("reconcile_diffs", sa.Column("note", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("reconcile_diffs", "note")
    op.drop_column("reconcile_runs", "counted_sku_count")
    op.drop_column("reconcile_runs", "scope_note")
    op.drop_column("reconcile_runs", "counted_on")
    op.drop_column("reconcile_runs", "counted_by")
    op.drop_column("reconcile_runs", "run_type")
