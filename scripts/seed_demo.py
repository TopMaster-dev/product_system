"""Populate the dev database with sample data for manual UI verification.

Run AFTER `alembic upgrade head` and BEFORE starting uvicorn:

    py scripts/seed_demo.py

Wipes existing rows in inventory tables (master_skus is kept idempotent).
"""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.models import Base, ChannelSkuMapping, MasterSku
from app.services import InventoryService

DEFAULT_URL = "postgresql+asyncpg://postgres:postgresql123@localhost:5432/product_system"


SAMPLE_SKUS = [
    # (sku_code, name, jan, shopify_sku, rakuten_sku, initial_qty)
    ("B50-GOLD-S", "シンプル B50 / Gold / S", "4901234560011", "10087goldS", "gold-s-B50", 25),
    ("B50-GOLD-M", "シンプル B50 / Gold / M", "4901234560028", "10087goldM", "gold-m-B50", 18),
    (
        "B50-SILVER-S",
        "シンプル B50 / Silver / S",
        "4901234560035",
        "10087silverS",
        "silver-s-B50",
        8,
    ),
    (
        "B50-SILVER-M",
        "シンプル B50 / Silver / M",
        "4901234560042",
        "10087silverM",
        "silver-m-B50",
        30,
    ),
    ("C12-BLUE", "クラシック C12 / Blue", "4901234560059", "20040blue", "blue-C12", 12),
    ("C12-RED", "クラシック C12 / Red", "4901234560066", "20040red", "red-C12", 4),  # low stock
    ("D03-PRO", "プロモデル D03", "4901234560073", "30100pro", "pro-D03", 0),
    ("E99-LEGACY", "レガシー E99 (販売終了)", "4901234560080", None, None, 5),  # no mappings
]


async def main() -> None:
    url = os.environ.get("DATABASE_URL", DEFAULT_URL)
    engine = create_async_engine(url, future=True)

    # Clean transactional tables (master SKUs persist across reseeds).
    async with engine.begin() as conn:
        table_names = ", ".join(
            t.name for t in reversed(Base.metadata.sorted_tables) if t.name != "alembic_version"
        )
        await conn.execute(text(f"TRUNCATE TABLE {table_names} RESTART IDENTITY CASCADE"))

    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as s, s.begin():
        # Insert master SKUs.
        skus = {}
        for code, name, jan, _shop, _rak, _qty in SAMPLE_SKUS:
            sku = MasterSku(sku_code=code, name=name, jan_code=jan)
            s.add(sku)
            skus[code] = sku
        await s.flush()

        # Insert channel mappings.
        for code, _name, _jan, shop_sku, rak_sku, _qty in SAMPLE_SKUS:
            master = skus[code]
            if shop_sku:
                s.add(
                    ChannelSkuMapping(
                        master_sku_id=master.id,
                        channel="shopify",
                        channel_sku=shop_sku,
                        is_active=True,
                    )
                )
            if rak_sku:
                s.add(
                    ChannelSkuMapping(
                        master_sku_id=master.id,
                        channel="rakuten",
                        channel_sku=rak_sku,
                        is_active=True,
                    )
                )

        # Seed initial inventory via manual_adjust so it shows up in the event log.
        inv = InventoryService(s)
        now = datetime.now(UTC)
        for code, _name, _jan, _shop, _rak, qty in SAMPLE_SKUS:
            if qty > 0:
                await inv.manual_adjust(
                    master_sku_id=skus[code].id,
                    quantity_delta=qty,
                    reason="initial stock (seed)",
                    operator="seed",
                    occurred_at=now,
                )

    await engine.dispose()
    print(f"Seeded {len(SAMPLE_SKUS)} master SKUs with mappings and initial inventory.")
    print("Notable cases:")
    print("  - C12-RED is intentionally low-stock (4 units)")
    print("  - D03-PRO is out-of-stock (0 units)")
    print("  - E99-LEGACY has no channel mappings (will trigger mapping alerts)")


if __name__ == "__main__":
    asyncio.run(main())
