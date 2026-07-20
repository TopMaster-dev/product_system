"""Integration tests — BundlePushService (batched derived-availability push)."""

from __future__ import annotations

import pytest

from app.models import BundleComponent, ChannelSkuMapping, MasterSku
from app.services import BundlePushService, EventSource, InventoryService

pytestmark = pytest.mark.integration


class _FakeAdapter:
    """Minimal ChannelAdapter double that records push_inventory calls."""

    channel = "shopify"

    def __init__(self) -> None:
        self.calls: list[tuple[str, int]] = []

    async def push_inventory(self, channel_sku: str, quantity: int) -> dict:
        self.calls.append((channel_sku, quantity))
        return {"ok": True}


async def _bundle(session, parent_code, comp_specs, *, shopify_sku=None):
    parent = MasterSku(sku_code=parent_code, name=parent_code, is_bundle=True)
    session.add(parent)
    await session.flush()
    if shopify_sku:
        session.add(
            ChannelSkuMapping(
                master_sku_id=parent.id, channel="shopify", channel_sku=shopify_sku, is_active=True
            )
        )
    inv = InventoryService(session)
    comps = []
    for code, stock in comp_specs:
        c = MasterSku(sku_code=code, name=code)
        session.add(c)
        await session.flush()
        if stock:
            await inv.manual_adjust(
                master_sku_id=c.id, quantity_delta=stock, reason="seed", operator="t"
            )
        session.add(
            BundleComponent(
                bundle_master_sku_id=parent.id, component_master_sku_id=c.id, quantity_per=1
            )
        )
        comps.append(c)
    await session.flush()
    return parent, comps


async def test_push_bundles_pushes_derived_availability(db_session) -> None:
    parent, _ = await _bundle(
        db_session, "N21gold", [("N23gold", 27), ("N32gold", 55)], shopify_sku="N21gold"
    )
    svc = BundlePushService(db_session)
    fake = _FakeAdapter()
    attempts = await svc.push_bundles(fake, [parent.id], triggered_by="test")
    assert fake.calls == [("N21gold", 27)]  # min(27, 55)
    assert len(attempts) == 1
    assert attempts[0].status == "succeeded"


async def test_push_bundles_skips_bundle_without_channel_mapping(db_session) -> None:
    parent, _ = await _bundle(db_session, "N21gold", [("N23gold", 5)], shopify_sku=None)
    svc = BundlePushService(db_session)
    fake = _FakeAdapter()
    attempts = await svc.push_bundles(fake, [parent.id], triggered_by="test")
    assert fake.calls == []
    assert attempts == []


async def test_dependent_bundle_ids_reverse_lookup(db_session) -> None:
    """A shared component (N23) resolves to every bundle that consumes it."""
    n23 = MasterSku(sku_code="N23gold", name="N23")
    db_session.add(n23)
    await db_session.flush()
    s1, _ = await _bundle(db_session, "N21gold", [], shopify_sku="N21gold")
    s2, _ = await _bundle(db_session, "N09gold", [], shopify_sku="N09gold")
    db_session.add_all(
        [
            BundleComponent(bundle_master_sku_id=s1.id, component_master_sku_id=n23.id),
            BundleComponent(bundle_master_sku_id=s2.id, component_master_sku_id=n23.id),
        ]
    )
    await db_session.flush()
    svc = BundlePushService(db_session)
    assert set(await svc.dependent_bundle_ids([n23.id])) == {s1.id, s2.id}


async def test_push_bundles_clamps_negative_component_to_zero(db_session) -> None:
    # A component driven NEGATIVE (order_consumed allows oversell) must advertise
    # 0, never a negative quantity.
    parent, (a, _b) = await _bundle(
        db_session, "N21gold", [("N23gold", 3), ("N32gold", 55)], shopify_sku="N21gold"
    )
    inv = InventoryService(db_session)
    await inv.consume_for_order_line(  # 3 - 5 = -2 (oversell)
        master_sku_id=a.id,
        quantity=5,
        source=EventSource(channel="shopify", order_id="O", line_id="L"),
    )
    svc = BundlePushService(db_session)
    fake = _FakeAdapter()
    await svc.push_bundles(fake, [parent.id], triggered_by="test")
    assert fake.calls == [("N21gold", 0)]  # min(-2, 55) -> clamped to 0
