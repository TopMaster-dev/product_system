"""add mapping_alerts.product_name (identify unmapped SKU by product name)

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-24

Client feedback: the alerts screen showed only a bare channel SKU, making it
hard to tell which product an unmapped-SKU alert was about. Capture the channel's
own product/item name (楽天=itemName / Shopify=line name) at ingest so operators
can recognize the product. Nullable + additive.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | Sequence[str] | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "mapping_alerts",
        sa.Column("product_name", sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("mapping_alerts", "product_name")
