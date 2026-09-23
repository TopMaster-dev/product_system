"""The damage queries against a real database.

Each test seeds one specific broken shape and asserts it is found, and the last
two assert that correct data is NOT flagged. Both directions matter: a report
that cries wolf gets ignored, and a report that finds nothing gets believed.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.cli.inspect_stock_event_damage import (
    find_unfanned_parent_events,
    find_unmanaged_events,
)
from app.models import (
    BundleComponent,
    InventoryEvent,
    InventoryEventTypeEnum,
    MasterSku,
)

pytestmark = pytest.mark.integration

WHEN = datetime(2026, 6, 1, tzinfo=UTC)


async def _master(session, sku_code: str, **kw) -> MasterSku:
    master = MasterSku(sku_code=sku_code, name=sku_code, **kw)
    session.add(master)
    await session.flush()
    return master


async def _event(
    session,
    master: MasterSku,
    *,
    delta: int = -2,
    order_id: str = "O-1",
    line_id: str = "L-1",
    event_type: str = InventoryEventTypeEnum.ORDER_CONSUMED,
) -> None:
    session.add(
        InventoryEvent(
            master_sku_id=master.id,
            event_type=event_type,
            quantity_delta=delta,
            source_channel="rakuten",
            source_order_id=order_id,
            source_line_id=line_id,
            occurred_at=WHEN,
        )
    )
    await session.flush()


async def _bundle(session, parent: MasterSku, component: MasterSku, qty: int = 1) -> None:
    session.add(
        BundleComponent(
            bundle_master_sku_id=parent.id,
            component_master_sku_id=component.id,
            quantity_per=qty,
        )
    )
    await session.flush()


# --- 在庫管理対象外 -------------------------------------------------------


async def test_an_order_event_on_an_unmanaged_master_is_reported(db_session) -> None:
    gift = await _master(db_session, "GIFT-BAG", is_stock_managed=False, non_inventory_kind="gift")
    await _event(db_session, gift, delta=-3)

    found = await find_unmanaged_events(db_session)

    assert [d.sku_code for d in found] == ["GIFT-BAG"]
    assert found[0].events == 1
    assert found[0].net_delta == -3
    assert found[0].note == "gift"


async def test_a_managed_master_with_order_events_is_not_reported(db_session) -> None:
    normal = await _master(db_session, "NORMAL-1")
    await _event(db_session, normal)

    assert await find_unmanaged_events(db_session) == []


async def test_a_manual_adjustment_on_an_unmanaged_master_is_not_damage(db_session) -> None:
    """Marking something 在庫管理対象外 and then zeroing its stock by hand is
    the cleanup, not the problem."""
    coupon = await _master(
        db_session, "COUPON-1", is_stock_managed=False, non_inventory_kind="coupon"
    )
    db_session.add(
        InventoryEvent(
            master_sku_id=coupon.id,
            event_type=InventoryEventTypeEnum.MANUAL_ADJUST,
            quantity_delta=-5,
            reason="在庫管理対象外への整理",
            operator="ops",
            occurred_at=WHEN,
        )
    )
    await db_session.flush()

    assert await find_unmanaged_events(db_session) == []


# --- 展開されなかった親 ---------------------------------------------------


async def test_a_parent_event_with_no_component_event_is_reported(db_session) -> None:
    """The shared-stock bug: the parent moved, the pool did not."""
    parent = await _master(db_session, "N108-PARENT", is_bundle=True)
    pool = await _master(db_session, "N108-POOL")
    await _bundle(db_session, parent, pool)
    await _event(db_session, parent, delta=-2)

    found = await find_unfanned_parent_events(db_session)

    assert [d.sku_code for d in found] == ["N108-PARENT"]
    assert found[0].net_delta == -2


async def test_a_correctly_fanned_out_order_is_not_reported(db_session) -> None:
    """The component carries the same source, which is the proof it expanded."""
    parent = await _master(db_session, "SET-A", is_bundle=True)
    component = await _master(db_session, "SET-A-PART")
    await _bundle(db_session, parent, component)
    await _event(db_session, component, delta=-2, order_id="O-OK", line_id="L-1")

    assert await find_unfanned_parent_events(db_session) == []


async def test_another_order_fanning_out_does_not_clear_a_broken_one(db_session) -> None:
    """Why the match is on the order line. One good sale must not vouch for a
    different, broken one on the same SKU."""
    parent = await _master(db_session, "SET-B", is_bundle=True)
    component = await _master(db_session, "SET-B-PART")
    await _bundle(db_session, parent, component)
    # A good order: only the component moved.
    await _event(db_session, component, order_id="O-GOOD", line_id="L-1")
    # A broken one: the parent moved and nothing reached the component.
    await _event(db_session, parent, order_id="O-BAD", line_id="L-1")

    found = await find_unfanned_parent_events(db_session)

    assert [d.sku_code for d in found] == ["SET-B"]
    assert found[0].events == 1


async def test_a_master_flagged_bundle_without_components_is_not_reported(db_session) -> None:
    """`is_bundle` can be set before the components exist. With nothing to fan
    out to, the master's own event is the correct one."""
    lonely = await _master(db_session, "BUNDLE-EMPTY", is_bundle=True)
    await _event(db_session, lonely)

    assert await find_unfanned_parent_events(db_session) == []


async def test_a_plain_sku_is_never_reported_as_an_unfanned_parent(db_session) -> None:
    plain = await _master(db_session, "PLAIN-1")
    await _event(db_session, plain)

    assert await find_unfanned_parent_events(db_session) == []


async def test_a_parent_cancellation_that_never_reached_the_pool_is_reported(
    db_session,
) -> None:
    """The mirror case. A credit that stopped at the parent leaves the pool
    short by exactly as much as the consume did."""
    parent = await _master(db_session, "SET-C", is_bundle=True)
    component = await _master(db_session, "SET-C-PART")
    await _bundle(db_session, parent, component)
    await _event(
        db_session,
        parent,
        delta=2,
        event_type=InventoryEventTypeEnum.CANCELLATION_RETURNED,
    )

    found = await find_unfanned_parent_events(db_session)

    assert [d.sku_code for d in found] == ["SET-C"]
    assert found[0].net_delta == 2
