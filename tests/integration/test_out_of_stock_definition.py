"""欠品は「在庫 0 以下」— どの画面でも同じ定義.

The dashboard counted exactly 0 while the risk screen counted ≤ 0, so the 59
negative-stock SKUs were out of stock on one screen and not on the other: three
screens, three figures (2026-10-03 responsive review).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select

from app.models import DailyKpiSnapshot, InventoryEvent, InventoryEventTypeEnum, MasterSku
from app.services.analytics_rollup import AnalyticsRollupService

pytestmark = pytest.mark.integration

DAY = date(2026, 9, 15)
BEFORE = datetime(2026, 9, 10, 3, 0, tzinfo=UTC)


async def _sku(session, code: str, *deltas: int) -> None:
    master = MasterSku(sku_code=code, name=code)
    session.add(master)
    await session.flush()
    for delta in deltas:
        session.add(
            InventoryEvent(
                master_sku_id=master.id,
                event_type=InventoryEventTypeEnum.MANUAL_ADJUST,
                quantity_delta=delta,
                occurred_at=BEFORE,
            )
        )
    await session.flush()


async def test_negative_stock_counts_as_out_of_stock(db_session) -> None:
    await _sku(db_session, "IN-STOCK", 5)
    await _sku(db_session, "ZERO")
    await _sku(db_session, "NEGATIVE", -3)

    await AnalyticsRollupService(db_session).rebuild_day(DAY)
    await db_session.flush()

    kpi = (
        await db_session.execute(select(DailyKpiSnapshot).where(DailyKpiSnapshot.stat_date == DAY))
    ).scalar_one()
    assert kpi.out_of_stock_sku_count == 2  # ZERO and NEGATIVE
    assert kpi.negative_stock_sku_count == 1
