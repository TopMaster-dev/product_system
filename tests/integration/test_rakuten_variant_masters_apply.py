"""SKU単位の商品を実際に作る部分 — 何を書き、何を書かないか.

`build_plan` is pure and covered by unit tests. What needs a real database is the
other half: that applying a plan writes exactly one master and one mapping row
per live Rakuten page, that it writes NO stock, that it leaves the page-level
mapping alone, and that running it twice does not map the same pair twice.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from app.cli.create_rakuten_variant_masters import (
    ACTION_CREATE,
    ACTION_SKIP,
    apply_plan,
    build_plan,
    load_state,
)
from app.models import ChannelSkuMapping, InventorySnapshot, MasterSku
from app.services.rakuten_catalogue import (
    PagePair,
    RakutenSku,
    group_variants,
    links_from_legacy_mappings,
)

pytestmark = pytest.mark.integration

PAIR = [PagePair("073", "073c")]
SKUS = [
    RakutenSku("073", "1435", "", 23, "リング", ("gold", "19cm")),
    RakutenSku("073c", "931", "", 29, "リング", ("gold", "19cm")),
]


async def _page_level_mapping(session, code: str = "073c") -> tuple[int, int]:
    """The row that resolves live Rakuten order lines today."""
    master = MasterSku(sku_code=code, name=code)
    session.add(master)
    await session.flush()
    mapping = ChannelSkuMapping(
        master_sku_id=master.id,
        channel="rakuten",
        channel_sku="073",
        channel_variant_key=None,
        is_active=True,
    )
    session.add(mapping)
    await session.flush()
    return master.id, mapping.id


async def _plan(session):
    state = await load_state(session)
    return build_plan(
        group_variants(SKUS, PAIR),
        proven={},
        master_by_code=state.master_by_code,
        category_of=state.category_of,
        already_mapped=state.already_mapped,
        category_by_page=state.category_by_page,
        legacy=links_from_legacy_mappings(SKUS, state.by_channel_sku),
    )


async def test_one_master_and_one_mapping_per_live_page(db_session) -> None:
    await _page_level_mapping(db_session)

    planned = await _plan(db_session)
    assert [item.action for item in planned] == [ACTION_CREATE]
    masters, mappings = await apply_plan(db_session, planned)
    await db_session.flush()

    assert (masters, mappings) == (1, 2)
    created = (
        await db_session.execute(select(MasterSku).where(MasterSku.sku_code == "073-19cm-gold"))
    ).scalar_one()
    rows = (
        (
            await db_session.execute(
                select(ChannelSkuMapping).where(ChannelSkuMapping.master_sku_id == created.id)
            )
        )
        .scalars()
        .all()
    )

    # Both pages point at the ONE master — that is the shared stock.
    assert {row.channel_sku for row in rows} == {"073", "073c"}
    assert {row.channel_variant_key for row in rows} == {"1435", "931"}
    assert all(row.is_active for row in rows)


async def test_no_stock_is_invented(db_session) -> None:
    """The count supplies the opening quantity. A number guessed here would
    later be written onto a live shop page."""
    await _page_level_mapping(db_session)
    await apply_plan(db_session, await _plan(db_session))
    await db_session.flush()

    created = (
        await db_session.execute(select(MasterSku).where(MasterSku.sku_code == "073-19cm-gold"))
    ).scalar_one()
    snapshot = await db_session.scalar(
        select(InventorySnapshot).where(InventorySnapshot.master_sku_id == created.id)
    )
    assert snapshot is None


async def test_the_page_level_mapping_is_left_untouched(db_session) -> None:
    """It is what resolves live Rakuten order lines; retiring it would turn every
    Rakuten line into a MappingAlert on the next poll."""
    page_master_id, mapping_id = await _page_level_mapping(db_session)

    await apply_plan(db_session, await _plan(db_session))
    await db_session.flush()

    original = await db_session.get(ChannelSkuMapping, mapping_id)
    assert original is not None
    assert original.is_active is True
    assert original.channel_variant_key is None
    assert original.master_sku_id == page_master_id


async def test_running_it_twice_maps_each_pair_once(db_session) -> None:
    await _page_level_mapping(db_session)
    await apply_plan(db_session, await _plan(db_session))
    await db_session.flush()

    second = await _plan(db_session)

    assert [item.action for item in second] == [ACTION_SKIP]
    masters, mappings = await apply_plan(db_session, second)
    assert (masters, mappings) == (0, 0)
    total = await db_session.scalar(
        select(func.count())
        .select_from(ChannelSkuMapping)
        .where(ChannelSkuMapping.channel_variant_key.is_not(None))
    )
    assert total == 2


async def test_a_new_master_inherits_the_category_of_the_product_it_came_from(db_session) -> None:
    """The real 115 case: two physical variants both sold under ONE page-level
    master, so neither is 1:1 and both need a master of their own. They must
    land in the same category as the product they came from — otherwise they are
    未分類 and a category-filtered count sheet omits them, and the shelf gets
    counted while the variants do not."""
    from app.models import ProductCategory

    category = ProductCategory(code="RING", name="リング", level=1)
    db_session.add(category)
    await db_session.flush()

    master = MasterSku(sku_code="073c", name="073c", category_id=category.id)
    db_session.add(master)
    await db_session.flush()
    db_session.add(
        ChannelSkuMapping(
            master_sku_id=master.id, channel="rakuten", channel_sku="073", is_active=True
        )
    )
    await db_session.flush()

    two_sizes = [
        *SKUS,
        RakutenSku("073", "1436", "", 18, "リング", ("gold", "20cm")),
        RakutenSku("073c", "932", "", 28, "リング", ("gold", "20cm")),
    ]
    state = await load_state(db_session)
    planned = build_plan(
        group_variants(two_sizes, PAIR),
        proven={("073", "1435"): master.id, ("073", "1436"): master.id},
        master_by_code=state.master_by_code,
        category_of=state.category_of,
        already_mapped=state.already_mapped,
        category_by_page=state.category_by_page,
    )
    assert [item.action for item in planned] == [ACTION_CREATE, ACTION_CREATE]

    await apply_plan(db_session, planned)
    await db_session.flush()

    created = (
        (
            await db_session.execute(
                select(MasterSku).where(MasterSku.sku_code.in_(["073-19cm-gold", "073-20cm-gold"]))
            )
        )
        .scalars()
        .all()
    )
    assert len(created) == 2
    for row in created:
        assert row.category_id == category.id
        assert row.attributes["rakuten_group"] == "073"
        assert row.attributes["from_master"] == "073c"
        assert row.is_stock_managed is True
        assert row.is_bundle is False


async def test_the_page_mapping_supplies_the_category_when_no_sale_has(db_session) -> None:
    """The route that carries the weight in production. 475 masters would be
    created and only a minority have a claimed master to inherit from; the rest
    reach their category through the page they are sold on. Measured against
    production 2026-10-19: 503 masters hold an active Rakuten page mapping and
    exactly one of them is uncategorised."""
    from app.models import ProductCategory

    category = ProductCategory(code="RING", name="リング", level=1)
    db_session.add(category)
    await db_session.flush()

    page_master = MasterSku(sku_code="073c", name="073c", category_id=category.id)
    db_session.add(page_master)
    await db_session.flush()
    db_session.add(
        ChannelSkuMapping(
            master_sku_id=page_master.id,
            channel="rakuten",
            channel_sku="073",
            channel_variant_key=None,
            is_active=True,
        )
    )
    await db_session.flush()

    # No `proven` links at all: nothing has ever sold, so there is no claimed
    # master and the page is the only source of a category.
    planned = await _plan(db_session)
    assert [item.action for item in planned] == [ACTION_CREATE]
    assert planned[0].category_id == category.id

    await apply_plan(db_session, planned)
    await db_session.flush()
    created = (
        await db_session.execute(select(MasterSku).where(MasterSku.sku_code == "073-19cm-gold"))
    ).scalar_one()
    assert created.category_id == category.id


async def test_an_inactive_page_mapping_does_not_supply_a_category(db_session) -> None:
    """A retired mapping is not evidence about where a variant belongs."""
    from app.models import ProductCategory

    category = ProductCategory(code="RING", name="リング", level=1)
    db_session.add(category)
    await db_session.flush()
    master = MasterSku(sku_code="073c", name="073c", category_id=category.id)
    db_session.add(master)
    await db_session.flush()
    db_session.add(
        ChannelSkuMapping(
            master_sku_id=master.id,
            channel="rakuten",
            channel_sku="073",
            channel_variant_key=None,
            is_active=False,
        )
    )
    await db_session.flush()

    planned = await _plan(db_session)
    assert planned[0].category_id is None
