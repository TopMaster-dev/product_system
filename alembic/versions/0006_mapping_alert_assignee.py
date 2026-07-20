"""add mapping_alerts.assignee (対応中 / in-progress owner)

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-20

Phase 1-B alert workflow: the admin alerts screen gains a 3-state flow
(未対応 open / 対応中 in_progress / 解決済み resolved). The `status` column is a
free string so `in_progress` needs no schema change; this migration only adds
the nullable `assignee` column recording who took an alert into 対応中.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "mapping_alerts",
        sa.Column("assignee", sa.String(length=128), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("mapping_alerts", "assignee")
