"""add master_skus.image_url (Shopify product thumbnail)

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-24

Client request: show the Shopify product image in the admin screens so staff can
recognize a product visually (inventory list, and when choosing which product an
unmapped channel SKU maps to). Populated by app.cli.sync_shopify_images.
Nullable + additive.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | Sequence[str] | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "master_skus",
        sa.Column("image_url", sa.String(length=1024), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("master_skus", "image_url")
