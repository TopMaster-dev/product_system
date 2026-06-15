"""Integration tests — InventoryService invariants against real Postgres.

Skipped automatically when the test DB is unreachable.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from app.models import InventoryEvent, InventoryEventTypeEnum, MasterSku
from app.services import (
    EventSource,
    InventoryInsufficientError,
    InventoryService,
    MasterSkuNotFoundError,
)

pytestmark = pytest.mark.integration


async def _make_sku(session, code: str = "TEST-001") -> MasterSku:
    sku = MasterSku(sku_code=code, name="Test SKU")
    session.add(sku)
    await session.flush()
    return sku


async def test_consume_decrements_snapshot(db_session) -> None:
    sku = await _make_sku(db_session)
    service = InventoryService(db_session)

    await service.manual_adjust(
        master_sku_id=sku.id, quantity_delta=10, reason="receipt", operator="op1"
    )
    await service.consume_for_order_line(
        master_sku_id=sku.id,
        quantity=3,
        source=EventSource(channel="shopify", order_id="O-1", line_id="L-1"),
    )

    assert await service.get_current_stock(sku.id) == 7


async def test_consume_is_idempotent(db_session) -> None:
    """Re-applying the same source identifiers must not double-decrement."""
    sku = await _make_sku(db_session)
    service = InventoryService(db_session)
    await service.manual_adjust(
        master_sku_id=sku.id, quantity_delta=10, reason="receipt", operator="op1"
    )
    src = EventSource(channel="shopify", order_id="O-1", line_id="L-1")

    first = await service.consume_for_order_line(master_sku_id=sku.id, quantity=3, source=src)
    second = await service.consume_for_order_line(master_sku_id=sku.id, quantity=3, source=src)

    assert first is not None
    assert second is None  # idempotent skip
    assert await service.get_current_stock(sku.id) == 7


async def test_cancellation_restores_stock(db_session) -> None:
    sku = await _make_sku(db_session)
    service = InventoryService(db_session)
    await service.manual_adjust(
        master_sku_id=sku.id, quantity_delta=5, reason="receipt", operator="op1"
    )
    src = EventSource(channel="rakuten", order_id="R-1", line_id="L-1")

    await service.consume_for_order_line(master_sku_id=sku.id, quantity=2, source=src)
    await service.cancel_order_line(master_sku_id=sku.id, quantity=2, source=src)

    assert await service.get_current_stock(sku.id) == 5


async def test_cancellation_is_idempotent(db_session) -> None:
    sku = await _make_sku(db_session)
    service = InventoryService(db_session)
    await service.manual_adjust(
        master_sku_id=sku.id, quantity_delta=5, reason="receipt", operator="op1"
    )
    src = EventSource(channel="rakuten", order_id="R-2", line_id="L-1")
    await service.consume_for_order_line(master_sku_id=sku.id, quantity=2, source=src)

    first = await service.cancel_order_line(master_sku_id=sku.id, quantity=2, source=src)
    second = await service.cancel_order_line(master_sku_id=sku.id, quantity=2, source=src)

    assert first is not None
    assert second is None
    assert await service.get_current_stock(sku.id) == 5


async def test_manual_adjust_rejects_negative_stock(db_session) -> None:
    sku = await _make_sku(db_session)
    service = InventoryService(db_session)
    await service.manual_adjust(
        master_sku_id=sku.id, quantity_delta=2, reason="initial", operator="op1"
    )
    with pytest.raises(InventoryInsufficientError):
        await service.manual_adjust(
            master_sku_id=sku.id, quantity_delta=-5, reason="bad", operator="op1"
        )


async def test_consume_allows_oversell_to_negative(db_session) -> None:
    """Order-driven events must NOT block on insufficient stock.

    Backorders/oversells are a business reality; the system records them
    accurately and the operator reconciles through manual_adjust.
    """
    sku = await _make_sku(db_session)
    service = InventoryService(db_session)
    await service.consume_for_order_line(
        master_sku_id=sku.id,
        quantity=3,
        source=EventSource(channel="shopify", order_id="O-1", line_id="L-1"),
    )
    assert await service.get_current_stock(sku.id) == -3


async def test_unknown_sku_raises(db_session) -> None:
    service = InventoryService(db_session)
    with pytest.raises(MasterSkuNotFoundError):
        await service.manual_adjust(
            master_sku_id=99999, quantity_delta=1, reason="x", operator="op1"
        )


async def test_event_records_source_and_operator(db_session) -> None:
    sku = await _make_sku(db_session)
    service = InventoryService(db_session)
    await service.manual_adjust(
        master_sku_id=sku.id,
        quantity_delta=5,
        reason="stocktake",
        operator="alice@example.com",
        occurred_at=datetime(2026, 5, 11, 12, 0, tzinfo=UTC),
    )

    result = await db_session.execute(
        select(InventoryEvent).where(InventoryEvent.master_sku_id == sku.id)
    )
    event = result.scalar_one()
    assert event.event_type == InventoryEventTypeEnum.MANUAL_ADJUST
    assert event.reason == "stocktake"
    assert event.operator == "alice@example.com"
    assert event.quantity_delta == 5
