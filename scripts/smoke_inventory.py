"""End-to-end smoke check for Sprint 1 — touches a real Postgres.

Run after `alembic upgrade head` on `product_system_test`:

    $env:TEST_DATABASE_URL = "postgresql+asyncpg://postgres:postgres@localhost:5432/product_system_test"
    py scripts/smoke_inventory.py

Exits non-zero on any invariant violation.
"""

from __future__ import annotations

import asyncio
import os
import sys

from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, MasterSku
from app.services import EventSource, InventoryService, MappingService


async def main() -> int:
    url = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://postgres:postgres@localhost:5432/product_system_test",
    )
    engine = create_async_engine(url, future=True)

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    Session = async_sessionmaker(engine, expire_on_commit=False)

    async with Session() as s, s.begin():
        sku = MasterSku(sku_code="SMOKE-1", name="Smoke SKU")
        s.add(sku)
        await s.flush()
        inv = InventoryService(s)

        await inv.manual_adjust(
            master_sku_id=sku.id, quantity_delta=10, reason="seed", operator="smoke"
        )
        assert (await inv.get_current_stock(sku.id)) == 10, "seed failed"
        print("[ok] seeded stock = 10")

        src = EventSource(channel="shopify", order_id="O-1", line_id="L-1")
        await inv.consume_for_order_line(master_sku_id=sku.id, quantity=3, source=src)
        assert (await inv.get_current_stock(sku.id)) == 7
        print("[ok] consumed 3 -> stock = 7")

        # Idempotency check: same source replays must not double-decrement.
        result = await inv.consume_for_order_line(master_sku_id=sku.id, quantity=3, source=src)
        assert result is None, "duplicate consume should be a no-op"
        assert (await inv.get_current_stock(sku.id)) == 7
        print("[ok] duplicate consume ignored -> stock still 7")

        # Cancellation compensates.
        await inv.cancel_order_line(master_sku_id=sku.id, quantity=3, source=src)
        assert (await inv.get_current_stock(sku.id)) == 10
        print("[ok] cancellation restored -> stock = 10")

    # Mapping replay scenario in a fresh session.
    async with Session() as s, s.begin():
        from datetime import UTC, datetime
        from decimal import Decimal

        from app.models import (
            MappingAlert,
            MappingAlertStatusEnum,
            Order,
            OrderItem,
            OrderStatusEnum,
        )

        master = MasterSku(sku_code="SMOKE-2", name="Replay")
        s.add(master)
        await s.flush()

        order = Order(
            channel="shopify",
            channel_order_id="O-PEND",
            status=OrderStatusEnum.PENDING_MAPPING,
            ordered_at=datetime.now(UTC),
        )
        s.add(order)
        await s.flush()
        s.add(
            OrderItem(
                order_id=order.id,
                line_id="L-1",
                channel_sku="UNMAPPED",
                quantity=2,
                unit_price=Decimal("1000"),
            )
        )
        s.add(
            MappingAlert(
                channel="shopify", channel_sku="UNMAPPED", status=MappingAlertStatusEnum.OPEN
            )
        )
        await s.flush()

        mapping = MappingService(s)
        replayed = await mapping.resolve_alert(
            channel="shopify", channel_sku="UNMAPPED", master_sku_id=master.id
        )
        assert replayed == 1, f"expected 1 replay, got {replayed}"
        stock = await InventoryService(s).get_current_stock(master.id)
        assert stock == -2, f"expected stock -2 (oversell visible), got {stock}"
        print(f"[ok] mapping resolution replayed 1 order, stock = {stock}")

    await engine.dispose()
    print("\nAll invariants verified.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
