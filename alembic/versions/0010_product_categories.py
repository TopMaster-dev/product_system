"""product_categories + master_skus.category_id

Revision ID: 0010
Revises: 0009
Create Date: 2026-08-25

Phase 2 W3. The 大分類/中分類 taxonomy every later analytic groups by.

Shape: a depth-2 adjacency list, not a general tree. The client's taxonomy is two
levels (ネックレス > クロス); an unbounded hierarchy would make every aggregate
carry a recursive CTE for depth that will never exist. Two CHECKs pin it:
`level BETWEEN 1 AND 2`, and `(level = 1) = (parent_id IS NULL)` which rejects
both a 大分類 with a parent and a 中分類 without one.

`(parent_id, name)` is UNIQUE **NULLS NOT DISTINCT** (PG15+, and prod is
POSTGRES_15). Postgres treats NULLs as distinct by default, so the plain form
would happily allow two top-level categories both named ネックレス — exactly the
duplicate this table exists to prevent.

`master_skus.category_id` is nullable: NULL means 未分類, which the screens show
as its own bucket rather than hiding. Nullable also means the running image keeps
working against this schema, so the usual order holds — migrate first, deploy
second.

Both FKs are ON DELETE RESTRICT. Deleting a category must not silently take its
children, nor blank the category of every SKU beneath it; the UI disables
categories instead (`is_active`), because historical rollups still reference them.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Same discipline as 0009: alembic wraps this in one transaction, so the
    # ACCESS EXCLUSIVE taken by the ALTER TABLE on master_skus is held to COMMIT.
    # On db-f1-micro with Rakuten polling every 5 minutes, a blocked ALTER also
    # queues every reader behind it. Fail fast and retry in a quiet window.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")

    op.create_table(
        "product_categories",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        # Stable analysis key. Names get re-worded as merchandising language
        # shifts; every rollup joins on `code` so a rename cannot re-bucket
        # history.
        sa.Column("code", sa.String(length=32), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("parent_id", sa.BigInteger(), nullable=True),
        sa.Column("level", sa.SmallInteger(), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("sort_order", sa.Integer(), server_default="0", nullable=False),
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
        sa.CheckConstraint("level BETWEEN 1 AND 2", name="ck_product_category_level"),
        sa.CheckConstraint(
            "(level = 1) = (parent_id IS NULL)", name="ck_product_category_root"
        ),
        sa.ForeignKeyConstraint(
            ["parent_id"], ["product_categories.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code", name="uq_product_category_code"),
    )
    # Alembic's UniqueConstraint has no NULLS NOT DISTINCT knob, so it is raw DDL.
    op.execute(
        "ALTER TABLE product_categories "
        "ADD CONSTRAINT uq_product_category_parent_name "
        "UNIQUE NULLS NOT DISTINCT (parent_id, name)"
    )
    op.create_index("ix_product_categories_parent", "product_categories", ["parent_id"])

    op.add_column("master_skus", sa.Column("category_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_master_skus_category",
        "master_skus",
        "product_categories",
        ["category_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    # Serves both カテゴリ別集計 and the 未分類件数 tile.
    op.create_index("ix_master_skus_category", "master_skus", ["category_id"])


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute("SET LOCAL statement_timeout = '120s'")

    op.drop_index("ix_master_skus_category", table_name="master_skus")
    op.drop_constraint("fk_master_skus_category", "master_skus", type_="foreignkey")
    op.drop_column("master_skus", "category_id")

    op.drop_index("ix_product_categories_parent", table_name="product_categories")
    op.drop_table("product_categories")
