"""推移CSVの受注件数 — 注文を1件として数える.

The trend summed `sku_daily_sales.order_count`, which counts orders per SKU, so
an order for two different products appeared twice in the 受注件数 column. The
channel-filtered CSV is what the client would hold against the 受注一覧 in RMS,
and every multi-product order would have shown up as a difference.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.models import MasterSku, Order, OrderItem, OrderStatusEnum, ProductCategory
from app.services.analytics_query import SalesFilter, bucketed_sales, period_kpis
from app.services.analytics_rollup import AnalyticsRollupService
from app.services.timeframe import Period

pytestmark = pytest.mark.integration

DAY = date(2026, 9, 15)
NOON = datetime(2026, 9, 15, 3, 0, tzinfo=UTC)
ONE_DAY = Period(DAY, DAY, "day")


async def _seed(session) -> dict[str, int]:
    rings = ProductCategory(code="RING", name="リング", level=1)
    chains = ProductCategory(code="CHAIN", name="チェーン", level=1)
    session.add_all([rings, chains])
    await session.flush()
    ring = MasterSku(sku_code="R1", name="R1", category_id=rings.id)
    ring2 = MasterSku(sku_code="R2", name="R2", category_id=rings.id)
    chain = MasterSku(sku_code="C1", name="C1", category_id=chains.id)
    session.add_all([ring, ring2, chain])
    await session.flush()

    async def order(order_id: str, channel: str, lines: list[tuple[int | None, int]]) -> None:
        o = Order(
            channel=channel,
            channel_order_id=order_id,
            status=OrderStatusEnum.CONFIRMED,
            ordered_at=NOON,
        )
        session.add(o)
        await session.flush()
        for i, (sku_id, qty) in enumerate(lines):
            session.add(
                OrderItem(
                    order_id=o.id,
                    line_id=f"L{i}",
                    channel_sku=f"{order_id}-{i}",
                    master_sku_id=sku_id,
                    quantity=qty,
                    unit_price=Decimal("1000"),
                )
            )
        await session.flush()

    # Seeded so the per-SKU sum and the distinct count DIVERGE everywhere —
    # with one multi-product order and one unmapped order they happened to
    # cancel out, and the first draft of this test passed on the broken code.
    # Old sum: 6 overall / 5 rakuten / 4 rings. Distinct: 4 / 3 / 3.
    await order("R-1", "rakuten", [(ring.id, 1), (chain.id, 1)])
    await order("R-2", "rakuten", [(None, 1)])  # unmapped only: still an order in RMS
    await order("R-3", "rakuten", [(ring.id, 1), (ring2.id, 1), (chain.id, 1)])
    await order("S-1", "shopify", [(ring.id, 2)])

    await AnalyticsRollupService(session).rebuild_day(DAY)
    await session.flush()
    return {"rings": rings.id, "chains": chains.id}


async def test_an_order_for_two_products_is_one_order(db_session) -> None:
    await _seed(db_session)

    (row,) = await bucketed_sales(db_session, ONE_DAY)
    kpis = await period_kpis(db_session, ONE_DAY)

    assert row.order_count == 4
    assert row.order_count == kpis.order_count  # the tile and the CSV agree


async def test_a_channel_counts_what_its_own_order_list_shows(db_session) -> None:
    await _seed(db_session)

    (rakuten,) = await bucketed_sales(db_session, ONE_DAY, where=SalesFilter(channel="rakuten"))
    (shopify,) = await bucketed_sales(db_session, ONE_DAY, where=SalesFilter(channel="shopify"))

    assert rakuten.order_count == 3  # R-1 and R-3 once each, R-2 although unmapped
    assert rakuten.quantity == 5
    assert shopify.order_count == 1


async def test_a_category_counts_the_orders_behind_its_sales(db_session) -> None:
    ids = await _seed(db_session)

    (rings,) = await bucketed_sales(
        db_session, ONE_DAY, where=SalesFilter(category_id=ids["rings"])
    )
    (chains,) = await bucketed_sales(
        db_session, ONE_DAY, where=SalesFilter(category_id=ids["chains"])
    )

    assert (rings.order_count, rings.quantity) == (3, 5)  # R-1, R-3 (two rings), S-1
    assert (chains.order_count, chains.quantity) == (2, 2)  # R-1, R-3


async def test_unclassified_with_no_such_sales_counts_no_orders(db_session) -> None:
    await _seed(db_session)
    assert (
        await bucketed_sales(db_session, ONE_DAY, where=SalesFilter(unclassified_only=True)) == []
    )
