"""Integration tests — deactivate_legacy_mappings scoping."""

from __future__ import annotations

import pytest

from app.cli.deactivate_legacy_mappings import legacy_mapping_ids
from app.models import ChannelSkuMapping, MasterSku

pytestmark = pytest.mark.integration


async def test_legacy_mapping_ids_scopes_to_migrated_products(db_session) -> None:
    # OLD product master 006c (migrated: its code appears in a crossmall key)
    old = MasterSku(sku_code="006c", name="old", attributes={})
    # NEW variant master (a crossmall target — never deactivated)
    variant = MasterSku(sku_code="N23gold", name="v", attributes={"token": "N23"})
    # OLD product master NOT migrated (no crossmall mapping references its code)
    unmigrated = MasterSku(sku_code="999c", name="u", attributes={})
    db_session.add_all([old, variant, unmigrated])
    await db_session.flush()

    old_shopify = ChannelSkuMapping(
        master_sku_id=old.id, channel="shopify", channel_sku="006cgoldanklet", is_active=True
    )
    old_rakuten = ChannelSkuMapping(
        master_sku_id=old.id, channel="rakuten", channel_sku="rk-006c", is_active=True
    )
    db_session.add_all(
        [
            old_shopify,
            old_rakuten,
            # crossmall mapping for the migrated code -> the variant master
            ChannelSkuMapping(
                master_sku_id=variant.id,
                channel="crossmall",
                channel_sku="006c|gold|",
                is_active=True,
            ),
            # the variant master's own shopify mapping (must NOT be deactivated)
            ChannelSkuMapping(
                master_sku_id=variant.id, channel="shopify", channel_sku="N23gold", is_active=True
            ),
            # an un-migrated legacy product (must NOT be deactivated)
            ChannelSkuMapping(
                master_sku_id=unmigrated.id, channel="shopify", channel_sku="999cx", is_active=True
            ),
        ]
    )
    await db_session.flush()

    ids = set(await legacy_mapping_ids(db_session))
    assert ids == {old_shopify.id, old_rakuten.id}  # only the migrated legacy product's mappings
