"""楽天SKU と自社商品の対応を、実際の受注から取り出す.

This shop's Rakuten product codes (`00037c`, `0020`) share no naming convention
with our master SKUs, so a code-matching join finds almost nothing — measured
2026-10-15: 0 of 1,107 Rakuten SKUs matched by システム連携用SKU番号. What does
exist is sales history: every Rakuten order line stores its SKU in the payload,
and the resolved lines already name the master. That makes the link a fact
rather than an inference, which is the only basis safe enough to push stock on.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from app.models import MasterSku, Order, OrderItem, OrderStatusEnum
from app.services.rakuten_catalogue import links_from_orders

pytestmark = pytest.mark.integration

WHEN = datetime(2026, 10, 1, 3, 0, tzinfo=UTC)


def _payload(*lines: tuple[str, str, str]) -> dict[str, Any]:
    """(itemDetailId, manageNumber, variantId), wrapped as RMS delivers it —
    purchaser fields included, so the test proves they are never read."""
    return {
        "OrdererModel": {"familyName": "山田", "phoneNumber1": "090-0000-0000"},
        "PackageModelList": [
            {
                "SenderModel": {"familyName": "山田", "zipCode1": "150"},
                "ItemModelList": [
                    {
                        "itemDetailId": detail,
                        "manageNumber": manage,
                        "SkuModelList": [{"variantId": variant, "merchantDefinedSkuId": ""}],
                    }
                    for detail, manage, variant in lines
                ],
            }
        ],
    }


async def _master(session, code: str) -> int:
    master = MasterSku(sku_code=code, name=code)
    session.add(master)
    await session.flush()
    return master.id


async def _order(
    session, order_id: str, payload: dict[str, Any], lines: list[tuple[str, int | None]]
):
    order = Order(
        channel="rakuten",
        channel_order_id=order_id,
        status=OrderStatusEnum.CONFIRMED,
        ordered_at=WHEN,
        raw_payload=payload,
    )
    session.add(order)
    await session.flush()
    for line_id, master_id in lines:
        session.add(
            OrderItem(
                order_id=order.id,
                line_id=line_id,
                channel_sku="unused",
                master_sku_id=master_id,
                quantity=1,
                unit_price=Decimal("1000"),
            )
        )
    await session.flush()
    return order


async def test_a_resolved_line_proves_one_sku_to_one_master(db_session) -> None:
    gold = await _master(db_session, "B20gold")
    silver = await _master(db_session, "B20silver")
    await _order(
        db_session,
        "R-1",
        _payload(("L0", "0020", "1399"), ("L1", "0020", "1400")),
        [("L0", gold), ("L1", silver)],
    )

    links = await links_from_orders(db_session)

    # Two SKUs of ONE product, correctly kept apart — the whole point of the pair key.
    assert links == {("0020", "1399"): gold, ("0020", "1400"): silver}


async def test_an_unresolved_line_teaches_nothing(db_session) -> None:
    """An order line we never mapped cannot name a master, and must not invent one."""
    await _order(db_session, "R-2", _payload(("L0", "00037", "1483")), [("L0", None)])
    assert await links_from_orders(db_session) == {}


async def test_a_remapped_sku_takes_the_version_seen_most(db_session) -> None:
    """A SKU that resolved to two masters over time means the mapping was
    corrected; the majority is the standing intent."""
    old = await _master(db_session, "OLD")
    new = await _master(db_session, "NEW")
    for i, master_id in enumerate([old, new, new]):
        await _order(db_session, f"R-3-{i}", _payload(("L0", "073", "1438")), [("L0", master_id)])

    assert await links_from_orders(db_session) == {("073", "1438"): new}


async def test_purchaser_fields_are_never_part_of_the_result(db_session) -> None:
    master = await _master(db_session, "K01")
    await _order(db_session, "R-4", _payload(("L0", "k01", "k01")), [("L0", master)])

    links = await links_from_orders(db_session)

    assert links == {("k01", "k01"): master}
    assert "山田" not in repr(links)


async def test_lines_from_other_channels_are_ignored(db_session) -> None:
    master = await _master(db_session, "S1")
    order = Order(
        channel="shopify",
        channel_order_id="S-1",
        status=OrderStatusEnum.CONFIRMED,
        ordered_at=WHEN,
        raw_payload=_payload(("L0", "0020", "1399")),
    )
    db_session.add(order)
    await db_session.flush()
    db_session.add(
        OrderItem(
            order_id=order.id,
            line_id="L0",
            channel_sku="x",
            master_sku_id=master,
            quantity=1,
            unit_price=Decimal("1"),
        )
    )
    await db_session.flush()

    assert await links_from_orders(db_session) == {}
