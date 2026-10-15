"""channel_variant_key — a mapping row can name the SKU inside a Rakuten page

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-16

Phase 2 Stage 1, P2-039.

`channel_sku` never had one meaning. On Shopify it is the variant SKU — the
finest unit the channel sells. On Rakuten it is 商品管理番号, a PAGE, and this
shop's catalogue is 100% 項目選択肢別在庫: measured over the 15 October RMS export,
419 products carry 1,107 SKUs, so a Rakuten mapping row today names up to twelve
sellable things at once. That is why the stock push could never be built: there
was nowhere to say WHICH size and colour a quantity belonged to.

`channel_variant_key` makes the grain explicit rather than implied. NULL means
"channel_sku is already the finest unit" — every existing row, and every Shopify,
Amazon and wholesale row for ever. Set, it is Rakuten's SKU管理番号.

WHY THE PAIR AND NOT THE SKU ALONE

132 SKU管理番号 values are reused across different products in that export. Keyed
on the SKU alone the push would be ambiguous for those; keyed on
(商品管理番号, SKU管理番号) all 1,107 rows are unique.

WHY THIS MIGRATION TOUCHES ZERO ROWS

No backfill. Which variant a Rakuten SKU belongs to is not derivable in SQL: the
1,107 SKUs fold into 587 physical variants only by way of the client's 198-pair
table and resolved order history (`app/services/rakuten_catalogue.py`), 227 of
those variants resolve to one of our masters, and 115 of the rest need a master
CREATED first. That work is a reviewed --dry-run CLI, not a migration.

No deactivation either. The existing page-level rows are what resolve live
Rakuten order lines (`app/services/ingest.py` `_lookup_mapping`), so retiring
them would turn every Rakuten line into a MappingAlert on the next hourly poll.
They stay, and a page can hold its page-level row and its variant rows at once.

WHY NULLS NOT DISTINCT IS LOAD-BEARING

Widening `uq_channel_sku_mapping` to four columns is only safe because the
constraint keeps NULLS NOT DISTINCT. Every existing row has a NULL
channel_variant_key, NULLs compare equal, so the four-column constraint is
exactly as strict as the three-column one for all current data: it cannot fail
on existing rows and cannot weaken today's guarantee. Under PostgreSQL's DEFAULT
(NULLS DISTINCT) the same widening would silently permit unlimited duplicate
(channel, channel_sku) mappings — the opposite of the intent.

The constraint name is kept so the ORM diff stays one line and
`scripts/check_migration_parity.py` stays quiet.

WHAT MUST SHIP WITH THIS

`app.services.mapping.resolve_master_sku_id`. Three code paths resolved a
mapping with `scalar_one_or_none()` keyed on (channel, channel_sku,
marketplace_id); the first variant row added beside a page-level row makes that
query return two rows, which raises MultipleResultsFound on the hourly poll and
becomes an HTTP 500 plus a critical Slack alert. The resolver pins
channel_variant_key, so a key-less lookup keeps matching exactly the one
page-level row it matches today.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

_UNIQUE = "uq_channel_sku_mapping"
_CHECK = "ck_channel_sku_mapping_variant_key_not_blank"
_TABLE = "channel_sku_mappings"


def upgrade() -> None:
    op.add_column(_TABLE, sa.Column("channel_variant_key", sa.String(length=128), nullable=True))
    op.drop_constraint(_UNIQUE, _TABLE, type_="unique")
    op.create_unique_constraint(
        _UNIQUE,
        _TABLE,
        ["channel", "channel_sku", "marketplace_id", "channel_variant_key"],
        postgresql_nulls_not_distinct=True,
    )
    op.create_check_constraint(
        _CHECK,
        _TABLE,
        "channel_variant_key IS NULL OR length(channel_variant_key) > 0",
    )


def downgrade() -> None:
    # Reversible only while no variant row exists: two variants of one page
    # collapse to the same three-column key, so the narrower constraint would be
    # violated. Refuse rather than drop live mapping rows to make room.
    offending = op.get_bind().execute(
        sa.text(f"SELECT count(*) FROM {_TABLE} WHERE channel_variant_key IS NOT NULL")
    ).scalar_one()
    if offending:
        raise RuntimeError(
            f"{offending} mapping rows carry a channel_variant_key; "
            "downgrading would merge distinct variants onto one key. "
            "Retire those rows deliberately first."
        )
    op.drop_constraint(_CHECK, _TABLE, type_="check")
    op.drop_constraint(_UNIQUE, _TABLE, type_="unique")
    op.create_unique_constraint(
        _UNIQUE,
        _TABLE,
        ["channel", "channel_sku", "marketplace_id"],
        postgresql_nulls_not_distinct=True,
    )
    op.drop_column(_TABLE, "channel_variant_key")
