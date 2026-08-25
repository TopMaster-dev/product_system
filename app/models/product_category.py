"""ProductCategory — the 大分類/中分類 taxonomy every Phase 2 analytic groups by.

A depth-2 adjacency list, not a general tree. The client's request is two levels
(ネックレス > クロス), and an unbounded hierarchy would force every aggregate to
carry a recursive CTE for depth it will never have. Two CHECK constraints hold
the shape:

* `level` is 1 or 2 — nothing deeper can be inserted;
* a level-1 row has no parent and a level-2 row must have one. Written as
  `(level = 1) = (parent_id IS NULL)`, which rejects both a rootless 大分類 and
  a 中分類 that escaped its parent.

`code` is the stable analysis key, and it matters more than it looks. Category
names get renamed in Japanese as merchandising language shifts, and every rollup
already written against a name would silently re-bucket. Reports join on `code`;
`name` is display only. The client CSV template says so in as many words.

`(parent_id, name)` is UNIQUE with **NULLS NOT DISTINCT** (PG15+). Postgres
treats NULLs as distinct by default, so without it two top-level categories could
both be called ネックレス — the parent_id NULLs would not collide and the
constraint would pass. That is precisely the duplicate this table must not allow.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

#: Depth of the taxonomy. Named rather than inlined so the CHECK, the model and
#: the UI all cite one number.
MAX_CATEGORY_LEVEL = 2


class ProductCategory(Base, TimestampMixin):
    __tablename__ = "product_categories"
    __table_args__ = (
        CheckConstraint(
            f"level BETWEEN 1 AND {MAX_CATEGORY_LEVEL}",
            name="ck_product_category_level",
        ),
        # Level and parentage cannot disagree: 大分類 has no parent, 中分類 must.
        CheckConstraint(
            "(level = 1) = (parent_id IS NULL)",
            name="ck_product_category_root",
        ),
        UniqueConstraint(
            "parent_id",
            "name",
            name="uq_product_category_parent_name",
            # Without this, two parentless 大分類 could share a name: Postgres
            # counts NULL parent_ids as distinct from each other.
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_product_categories_parent", "parent_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    #: Stable key for analytics and the SKU-assignment CSV. Never re-used.
    code: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    parent_id: Mapped[int | None] = mapped_column(
        BigInteger,
        # RESTRICT, not CASCADE: deleting a 大分類 must not silently take its
        # children — and with them the category of every SKU underneath.
        ForeignKey("product_categories.id", ondelete="RESTRICT"),
        nullable=True,
    )
    level: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    #: Soft disable. Categories are referenced by historical rollups, so they are
    #: retired rather than deleted — the same reasoning as `archived_at` on
    #: master_skus (see app.services.sku_scope).
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("true"), default=True
    )
    #: Display order within a parent; ties fall back to `code`.
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0", default=0)
