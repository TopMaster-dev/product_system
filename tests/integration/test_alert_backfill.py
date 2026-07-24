"""Integration test — backfill_alert_product_names fills existing alerts from
the originating order's stored raw_payload."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.cli.backfill_alert_product_names import run
from app.models import MappingAlert, Order

pytestmark = pytest.mark.integration


async def test_backfill_fills_product_name_from_order_payload(_test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    async with factory() as session, session.begin():
        # An old alert with no product name, and the order that triggered it.
        session.add(
            MappingAlert(channel="rakuten", channel_sku="10113", status="open", product_name=None)
        )
        session.add(
            Order(
                channel="rakuten",
                channel_order_id="R-1",
                status="pending_mapping",
                ordered_at=datetime(2026, 5, 27, tzinfo=UTC),
                raw_payload={
                    "PackageModelList": [
                        {
                            "ItemModelList": [
                                {"manageNumber": "10113", "itemName": "馬蹄ネックレス gold"}
                            ]
                        }
                    ]
                },
            )
        )
        # An alert whose order isn't present stays null (still_missing).
        session.add(
            MappingAlert(channel="rakuten", channel_sku="99999", status="open", product_name=None)
        )

    await run(dry_run=False, session_factory=factory)

    async with factory() as session:
        filled = (
            await session.execute(select(MappingAlert).where(MappingAlert.channel_sku == "10113"))
        ).scalar_one()
        assert filled.product_name == "馬蹄ネックレス gold"
        missing = (
            await session.execute(select(MappingAlert).where(MappingAlert.channel_sku == "99999"))
        ).scalar_one()
        assert missing.product_name is None
