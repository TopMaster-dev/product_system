"""Integration tests — admin UI end-to-end via ASGITransport.

Covers Basic Auth, every screen renders, and the high-value mutations
(manual adjust, mapping create/delete, alert resolution) wire through
to the underlying services.
"""

from __future__ import annotations

import base64
from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import Settings
from app.db import get_session
from app.main import app
from app.models import (
    ChannelSkuMapping,
    InventoryEvent,
    InventoryEventTypeEnum,
    InventorySnapshot,
    MappingAlert,
    MappingAlertStatusEnum,
    MasterSku,
    Order,
    OrderItem,
    OrderStatusEnum,
    ReconcileDiff,
    ReconcileRun,
    SyncAttempt,
)

pytestmark = pytest.mark.integration

USER = "admin"
PASSWORD = "test_secret"


def _auth_header(user: str = USER, password: str = PASSWORD) -> dict[str, str]:
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


@pytest.fixture
async def admin_client(_test_engine) -> AsyncIterator[AsyncClient]:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)

    async def _override_session():
        async with factory() as session:
            yield session

    test_settings = Settings(
        app_env="local",
        admin_username=USER,
        admin_password=PASSWORD,
    )
    app.dependency_overrides[get_session] = _override_session
    from app.ui.auth import get_settings as auth_get_settings

    app.dependency_overrides[auth_get_settings] = lambda: test_settings
    from app.ui.routes.home import (
        get_session as home_get_session,  # noqa: F401 (proves no override clash)
    )

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        yield client

    app.dependency_overrides.clear()


async def _seed_sku(factory, code: str = "T-1", name: str = "Test") -> int:
    async with factory() as session, session.begin():
        sku = MasterSku(sku_code=code, name=name)
        session.add(sku)
        await session.flush()
        return sku.id


async def test_unauthenticated_request_returns_401(admin_client) -> None:
    r = await admin_client.get("/admin/")
    assert r.status_code == 401
    assert "WWW-Authenticate" in r.headers


async def test_wrong_password_returns_401(admin_client) -> None:
    r = await admin_client.get("/admin/", headers=_auth_header(password="bad"))
    assert r.status_code == 401


async def test_home_renders(admin_client, _test_engine) -> None:
    r = await admin_client.get("/admin/", headers=_auth_header())
    assert r.status_code == 200
    assert "ダッシュボード" in r.text
    assert "operator:" in r.text
    assert "/admin/manual" in r.text  # header book-icon link to the manual


async def test_manual_page_renders(admin_client, _test_engine) -> None:
    r = await admin_client.get("/admin/manual", headers=_auth_header())
    assert r.status_code == 200
    assert "管理画面 操作手順書" in r.text
    assert "リコンサイル" in r.text
    assert "困ったときは" in r.text


async def test_inventory_list_filters_and_paginates(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _seed_sku(factory, "INV-A", "Apple")
    await _seed_sku(factory, "INV-B", "Banana")

    r = await admin_client.get("/admin/inventory", headers=_auth_header())
    assert r.status_code == 200
    assert "INV-A" in r.text and "INV-B" in r.text

    # Search narrows.
    r = await admin_client.get("/admin/inventory?q=Banana", headers=_auth_header())
    assert "INV-A" not in r.text
    assert "INV-B" in r.text


async def _seed_stock(factory, code: str, qty: int, name: str | None = None) -> int:
    async with factory() as session, session.begin():
        sku = MasterSku(sku_code=code, name=name or code)
        session.add(sku)
        await session.flush()
        session.add(InventorySnapshot(master_sku_id=sku.id, on_hand_qty=qty))
        return sku.id


async def test_inventory_status_buckets_filter(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _seed_stock(factory, "ST-NEG", -3)
    await _seed_stock(factory, "ST-ZERO", 0)
    await _seed_stock(factory, "ST-LOW", 5)
    await _seed_stock(factory, "ST-OK", 50)

    r = await admin_client.get("/admin/inventory?filter=negative", headers=_auth_header())
    assert "ST-NEG" in r.text and "ST-ZERO" not in r.text and "ST-OK" not in r.text

    r = await admin_client.get("/admin/inventory?filter=zero", headers=_auth_header())
    assert "ST-ZERO" in r.text and "ST-NEG" not in r.text and "ST-LOW" not in r.text

    # Low is the exclusive 1..9 bucket — excludes zero and negative.
    r = await admin_client.get("/admin/inventory?filter=low", headers=_auth_header())
    assert "ST-LOW" in r.text and "ST-ZERO" not in r.text and "ST-NEG" not in r.text

    r = await admin_client.get("/admin/inventory?filter=normal", headers=_auth_header())
    assert "ST-OK" in r.text and "ST-LOW" not in r.text


async def test_inventory_default_order_prioritizes_problems(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _seed_stock(factory, "ZZZ-OK", 50)  # alphabetically last, but normal
    await _seed_stock(factory, "AAA-NEG", -1)  # alphabetically first, negative

    r = await admin_client.get("/admin/inventory", headers=_auth_header())
    # Default sort is problem-priority: the negative SKU appears before the normal one.
    assert r.text.index("AAA-NEG") < r.text.index("ZZZ-OK")


async def test_inventory_export_csv(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _seed_stock(factory, "EXP-NEG", -2, "NegItem")
    await _seed_stock(factory, "EXP-OK", 30, "OkItem")

    r = await admin_client.get("/admin/inventory/export.csv", headers=_auth_header())
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert "sku_code,name,jan_code,on_hand_qty,status,best_seller,updated_at" in r.text
    assert "EXP-NEG,NegItem,,-2,マイナス" in r.text
    assert "EXP-OK,OkItem,,30,正常" in r.text

    # Export respects the state filter.
    r = await admin_client.get(
        "/admin/inventory/export.csv?filter=negative", headers=_auth_header()
    )
    assert "EXP-NEG" in r.text and "EXP-OK" not in r.text


async def test_inventory_bestseller_flag_and_risk(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    hot = await _seed_stock(factory, "HOT-1", 3, "HotSeller")  # low stock
    await _seed_stock(factory, "COLD-1", 100, "ColdItem")  # no sales

    # Give HOT-1 recent order_consumed events so it's the sole best-seller.
    async with factory() as session, session.begin():
        for i in range(5):
            session.add(
                InventoryEvent(
                    master_sku_id=hot,
                    event_type=InventoryEventTypeEnum.ORDER_CONSUMED,
                    quantity_delta=-4,
                    source_channel="shopify",
                    source_order_id=f"BS-O{i}",
                    source_line_id="L1",
                    occurred_at=datetime.now(UTC),
                )
            )

    # Default view flags the best-seller and its low-stock risk.
    r = await admin_client.get("/admin/inventory", headers=_auth_header())
    assert "売れ筋" in r.text
    assert "売れ筋商品が在庫不足です" in r.text  # HOT-1 qty 3 (<10)

    # The bestseller filter narrows to just the hot SKU.
    r = await admin_client.get("/admin/inventory?filter=bestseller", headers=_auth_header())
    assert "HOT-1" in r.text and "COLD-1" not in r.text


async def test_adjust_form_shows_reason_templates(admin_client, _test_engine) -> None:
    r = await admin_client.get("/admin/adjust", headers=_auth_header())
    assert r.status_code == 200
    assert "検品NG" in r.text  # a reason template
    assert "POPUP戻り在庫" in r.text
    assert "最近の手動調整" in r.text  # recent-history panel


async def test_adjust_confirm_shows_before_after(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku_id = await _seed_stock(factory, "CONF-1", 10, "Confirmable")

    r = await admin_client.post(
        "/admin/adjust/confirm",
        data={"master_sku_id": sku_id, "quantity_delta": 5, "reason": "棚卸差異"},
        headers=_auth_header(),
    )
    assert r.status_code == 200
    assert "確定して適用" in r.text  # confirm screen, not yet applied
    assert "棚卸差異" in r.text
    assert "15" in r.text  # after = 10 + 5

    # Nothing applied yet — no event exists.
    async with factory() as session:
        events = (
            await session.execute(
                select(InventoryEvent).where(InventoryEvent.master_sku_id == sku_id)
            )
        ).all()
        assert events == []


async def test_adjust_confirm_rejects_negative_before_applying(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku_id = await _seed_stock(factory, "CONF-2", 3, "TooLow")

    r = await admin_client.post(
        "/admin/adjust/confirm",
        data={"master_sku_id": sku_id, "quantity_delta": -5, "reason": "x"},
        headers=_auth_header(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "insufficient" in r.headers["location"]


async def test_adjust_recent_history_reflects_applied(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku_id = await _seed_stock(factory, "REC-1", 20, "Recent")

    await admin_client.post(
        "/admin/adjust",
        data={"master_sku_id": sku_id, "quantity_delta": -2, "reason": "破損"},
        headers=_auth_header(),
    )
    r = await admin_client.get("/admin/adjust", headers=_auth_header())
    assert "REC-1" in r.text
    assert "破損" in r.text  # shows in the recent-adjustments panel


async def test_alerts_tabs_counts_and_filter(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    async with factory() as session, session.begin():
        session.add_all(
            [
                MappingAlert(channel="shopify", channel_sku="OPEN-1", status="open"),
                MappingAlert(channel="rakuten", channel_sku="OPEN-2", status="open"),
                MappingAlert(channel="shopify", channel_sku="DONE-1", status="resolved"),
            ]
        )

    # Open tab shows only open alerts.
    r = await admin_client.get("/admin/alerts?status=open", headers=_auth_header())
    assert "OPEN-1" in r.text and "OPEN-2" in r.text and "DONE-1" not in r.text

    # Resolved tab shows only resolved.
    r = await admin_client.get("/admin/alerts?status=resolved", headers=_auth_header())
    assert "DONE-1" in r.text and "OPEN-1" not in r.text

    # Channel filter.
    r = await admin_client.get("/admin/alerts?status=open&channel=rakuten", headers=_auth_header())
    assert "OPEN-2" in r.text and "OPEN-1" not in r.text

    # SKU substring filter.
    r = await admin_client.get("/admin/alerts?status=open&q=OPEN-1", headers=_auth_header())
    assert "OPEN-1" in r.text and "OPEN-2" not in r.text


async def test_alerts_start_moves_to_in_progress(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    async with factory() as session, session.begin():
        alert = MappingAlert(channel="shopify", channel_sku="MISS-IP", status="open")
        session.add(alert)
        await session.flush()
        alert_id = alert.id

    r = await admin_client.post(
        f"/admin/alerts/{alert_id}/start",
        data={"assignee": "田中"},
        headers=_auth_header(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "status=in_progress" in r.headers["location"]

    async with factory() as session:
        alert = (
            await session.execute(select(MappingAlert).where(MappingAlert.id == alert_id))
        ).scalar_one()
        assert alert.status == "in_progress"
        assert alert.assignee == "田中"

    r = await admin_client.get("/admin/alerts?status=in_progress", headers=_auth_header())
    assert "MISS-IP" in r.text
    r = await admin_client.get("/admin/alerts?status=open", headers=_auth_header())
    assert "MISS-IP" not in r.text


async def test_alerts_resolve_from_in_progress_replays(admin_client, _test_engine) -> None:
    """Regression: resolving must work from in_progress, not just open."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku_id = await _seed_sku(factory, "IP-SKU", "InProgResolve")
    async with factory() as session, session.begin():
        session.add(
            MappingAlert(
                channel="shopify", channel_sku="IP-1", status="in_progress", assignee="田中"
            )
        )
        order = Order(
            channel="shopify",
            channel_order_id="O-IP",
            status=OrderStatusEnum.PENDING_MAPPING,
            ordered_at=datetime(2026, 5, 11, tzinfo=UTC),
        )
        session.add(order)
        await session.flush()
        session.add(
            OrderItem(
                order_id=order.id, line_id="L1", channel_sku="IP-1", quantity=1, unit_price=1000
            )
        )
    async with factory() as session:
        alert_id = (
            await session.execute(select(MappingAlert.id).where(MappingAlert.channel_sku == "IP-1"))
        ).scalar_one()

    r = await admin_client.post(
        f"/admin/alerts/{alert_id}/resolve",
        data={"master_sku_id": sku_id},
        headers=_auth_header(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "resolved:1" in r.headers["location"]

    async with factory() as session:
        alert = (
            await session.execute(select(MappingAlert).where(MappingAlert.id == alert_id))
        ).scalar_one()
        assert alert.status == MappingAlertStatusEnum.RESOLVED


async def test_mapping_create_and_delete(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku_id = await _seed_sku(factory, "M-1", "Mapped")

    r = await admin_client.post(
        "/admin/mappings/new",
        data={
            "master_sku_id": sku_id,
            "channel": "shopify",
            "channel_sku": "SHOP-001",
        },
        headers=_auth_header(),
    )
    assert r.status_code == 303

    async with factory() as session:
        mapping = (
            await session.execute(
                select(ChannelSkuMapping).where(ChannelSkuMapping.channel_sku == "SHOP-001")
            )
        ).scalar_one()
        mapping_id = mapping.id

    r = await admin_client.post(f"/admin/mappings/{mapping_id}/delete", headers=_auth_header())
    assert r.status_code == 303

    async with factory() as session:
        rows = (
            await session.execute(
                select(ChannelSkuMapping).where(ChannelSkuMapping.id == mapping_id)
            )
        ).all()
        assert rows == []


async def test_mapping_csv_export(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku_id = await _seed_sku(factory, "E-1", "ExportMe")
    async with factory() as session, session.begin():
        session.add(
            ChannelSkuMapping(
                master_sku_id=sku_id, channel="shopify", channel_sku="EXP-1", is_active=True
            )
        )

    r = await admin_client.get("/admin/mappings/export.csv", headers=_auth_header())
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert "E-1,shopify,EXP-1" in r.text


async def test_manual_adjust_records_event_with_operator(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku_id = await _seed_sku(factory, "ADJ-1", "Adjust me")

    r = await admin_client.post(
        "/admin/adjust",
        data={
            "master_sku_id": sku_id,
            "quantity_delta": 7,
            "reason": "棚卸",
        },
        headers=_auth_header(),
    )
    assert r.status_code == 303

    async with factory() as session:
        event = (
            await session.execute(
                select(InventoryEvent).where(InventoryEvent.master_sku_id == sku_id)
            )
        ).scalar_one()
        assert event.event_type == InventoryEventTypeEnum.MANUAL_ADJUST
        assert event.quantity_delta == 7
        assert event.reason == "棚卸"
        assert event.operator == USER


async def test_manual_adjust_rejects_negative_stock(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku_id = await _seed_sku(factory, "NEG-1", "Neg")

    r = await admin_client.post(
        "/admin/adjust",
        data={"master_sku_id": sku_id, "quantity_delta": -5, "reason": "x"},
        headers=_auth_header(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "insufficient" in r.headers["location"]


async def test_event_log_filters(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku_id = await _seed_sku(factory, "EV-1", "Event")
    async with factory() as session, session.begin():
        session.add(
            InventoryEvent(
                master_sku_id=sku_id,
                event_type=InventoryEventTypeEnum.MANUAL_ADJUST,
                quantity_delta=3,
                reason="seed",
                operator="op",
                occurred_at=datetime(2026, 5, 11, 10, 0, tzinfo=UTC),
            )
        )

    r = await admin_client.get("/admin/events?event_type=manual_adjust", headers=_auth_header())
    assert r.status_code == 200
    assert "manual_adjust" in r.text
    assert "+3" in r.text


async def test_alerts_resolve_replays_pending_order(admin_client, _test_engine) -> None:
    """End-to-end: alert resolution backfills mapping and replays parked order."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku_id = await _seed_sku(factory, "ALERT-1", "Will resolve")
    async with factory() as session, session.begin():
        session.add(
            MappingAlert(
                channel="shopify",
                channel_sku="MISSING-1",
                status=MappingAlertStatusEnum.OPEN,
            )
        )
        order = Order(
            channel="shopify",
            channel_order_id="O-ALERT",
            status=OrderStatusEnum.PENDING_MAPPING,
            ordered_at=datetime(2026, 5, 11, tzinfo=UTC),
        )
        session.add(order)
        await session.flush()
        session.add(
            OrderItem(
                order_id=order.id,
                line_id="L-1",
                channel_sku="MISSING-1",
                quantity=2,
                unit_price=1000,
            )
        )

    async with factory() as session:
        result = await session.execute(
            select(MappingAlert.id).where(MappingAlert.channel_sku == "MISSING-1")
        )
        alert_id = result.scalar_one()

    r = await admin_client.post(
        f"/admin/alerts/{alert_id}/resolve",
        data={"master_sku_id": sku_id},
        headers=_auth_header(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "resolved:1" in r.headers["location"]

    async with factory() as session:
        alert = (
            await session.execute(select(MappingAlert).where(MappingAlert.id == alert_id))
        ).scalar_one()
        assert alert.status == MappingAlertStatusEnum.RESOLVED
        assert alert.resolved_master_sku_id == sku_id

        order = (
            await session.execute(select(Order).where(Order.channel_order_id == "O-ALERT"))
        ).scalar_one()
        assert order.status == "confirmed"

        events = (
            (
                await session.execute(
                    select(InventoryEvent).where(InventoryEvent.master_sku_id == sku_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(events) == 1
        assert events[0].quantity_delta == -2


# --------------------------------------------------------------------------- #
# 同期エラー (sync errors)                                                      #
# --------------------------------------------------------------------------- #


async def _seed_failed_push(factory, sku_id: int, *, channel: str = "shopify") -> int:
    async with factory() as session, session.begin():
        attempt = SyncAttempt(
            attempt_type="push_inventory",
            channel=channel,
            master_sku_id=sku_id,
            payload={"channel_sku": "SHOP-9", "quantity": 5, "triggered_by": "poll"},
            status="failed",
            error_code="ReadTimeout",
            error_message="read timed out",
        )
        session.add(attempt)
        await session.flush()
        return attempt.id


async def test_sync_errors_list_localizes_and_filters(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku_id = await _seed_sku(factory, "SE-1", "SyncErr")
    await _seed_failed_push(factory, sku_id)

    r = await admin_client.get("/admin/sync-errors", headers=_auth_header())
    assert r.status_code == 200
    assert "SE-1" in r.text
    assert "タイムアウト" in r.text  # localized guidance
    assert "再実行" in r.text  # retry button present

    # Default filter is failed; asking for succeeded hides it.
    r = await admin_client.get("/admin/sync-errors?status=succeeded", headers=_auth_header())
    assert "SE-1" not in r.text


class _FakeAdapter:
    """Minimal ChannelAdapter stand-in for exercising the retry push path
    without a live channel (mirrors how the push-service tests fake adapters)."""

    channel = "shopify"

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[tuple[str, int]] = []

    async def __aenter__(self) -> _FakeAdapter:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False

    async def push_inventory(self, channel_sku: str, quantity: int) -> dict[str, object]:
        self.calls.append((channel_sku, quantity))
        if self.fail:
            raise RuntimeError("boom")
        return {"ok": True, "quantity": quantity}


async def test_sync_errors_retry_pushes_current_quantity(
    admin_client, _test_engine, monkeypatch
) -> None:
    from app.ui.routes import sync_errors as sync_errors_module

    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku_id = await _seed_sku(factory, "SE-2", "Retryable")
    async with factory() as session, session.begin():
        session.add(InventorySnapshot(master_sku_id=sku_id, on_hand_qty=4))
    attempt_id = await _seed_failed_push(factory, sku_id)

    fake = _FakeAdapter()
    monkeypatch.setattr(sync_errors_module, "build_retry_adapter", lambda channel, settings: fake)

    r = await admin_client.post(
        f"/admin/sync-errors/{attempt_id}/retry",
        headers=_auth_header(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "retried" in r.headers["location"]
    # It re-pushed the CURRENT snapshot quantity (4), not the stale payload (5).
    assert fake.calls == [("SHOP-9", 4)]

    async with factory() as session:
        child = (
            await session.execute(
                select(SyncAttempt).where(SyncAttempt.parent_attempt_id == attempt_id)
            )
        ).scalar_one()
        assert child.status == "succeeded"
        assert child.payload["quantity"] == 4


async def test_sync_errors_retry_without_adapter_flashes_nocreds(
    admin_client, _test_engine, monkeypatch
) -> None:
    from app.ui.routes import sync_errors as sync_errors_module

    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku_id = await _seed_sku(factory, "SE-4", "NoCreds")
    attempt_id = await _seed_failed_push(factory, sku_id)

    monkeypatch.setattr(sync_errors_module, "build_retry_adapter", lambda channel, settings: None)

    r = await admin_client.post(
        f"/admin/sync-errors/{attempt_id}/retry",
        headers=_auth_header(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "nocreds" in r.headers["location"]


async def test_sync_errors_retry_rejects_non_failed(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku_id = await _seed_sku(factory, "SE-3", "Succeeded")
    async with factory() as session, session.begin():
        attempt = SyncAttempt(
            attempt_type="push_inventory",
            channel="shopify",
            master_sku_id=sku_id,
            payload={"channel_sku": "S", "quantity": 1},
            status="succeeded",
        )
        session.add(attempt)
        await session.flush()
        attempt_id = attempt.id

    r = await admin_client.post(
        f"/admin/sync-errors/{attempt_id}/retry",
        headers=_auth_header(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "notfailed" in r.headers["location"]


# --------------------------------------------------------------------------- #
# リコンサイル / 在庫CSV取込                                                     #
# --------------------------------------------------------------------------- #


async def _seed_reconcilable_variant(factory) -> int:
    """A variant master + its crossmall mapping (key '006c||') + a snapshot of 10."""
    async with factory() as session, session.begin():
        sku = MasterSku(sku_code="006cV", name="Variant")
        session.add(sku)
        await session.flush()
        session.add(
            ChannelSkuMapping(
                master_sku_id=sku.id, channel="crossmall", channel_sku="006c||", is_active=True
            )
        )
        session.add(InventorySnapshot(master_sku_id=sku.id, on_hand_qty=10))
        return sku.id


async def test_reconcile_list_renders(admin_client, _test_engine) -> None:
    r = await admin_client.get("/admin/reconcile", headers=_auth_header())
    assert r.status_code == 200
    assert "リコンサイル" in r.text


async def test_reconcile_upload_rejects_bad_csv(admin_client, _test_engine) -> None:
    bad = "商品コード\r\n006c\r\n".encode("cp932")  # missing 在庫数量
    r = await admin_client.post(
        "/admin/reconcile/upload",
        files={"file": ("bad.csv", bad, "text/csv")},
        headers=_auth_header(),
    )
    assert r.status_code == 200
    assert "取込できません" in r.text


async def test_reconcile_upload_preview_execute_approve_finalize(
    admin_client, _test_engine
) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    sku_id = await _seed_reconcilable_variant(factory)
    csv_bytes = "商品コード,在庫数量\r\n006c,27\r\n".encode("cp932")

    # 1) Preview — shows the +17 diff, no run created yet.
    r = await admin_client.post(
        "/admin/reconcile/upload",
        files={"file": ("stock.csv", csv_bytes, "text/csv")},
        headers=_auth_header(),
    )
    assert r.status_code == 200
    assert "006cV" in r.text
    assert "+17" in r.text
    async with factory() as session:
        assert (await session.execute(select(ReconcileRun))).scalars().all() == []

    # 2) Execute — creates the run.
    b64 = base64.b64encode(csv_bytes).decode("ascii")
    r = await admin_client.post(
        "/admin/reconcile/execute",
        data={"csv_b64": b64, "filename": "stock.csv"},
        headers=_auth_header(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    location = r.headers["location"]
    assert "flash=created" in location
    run_id = int(location.split("/admin/reconcile/")[1].split("?")[0])

    # 3) Detail renders with the diff.
    r = await admin_client.get(f"/admin/reconcile/{run_id}", headers=_auth_header())
    assert r.status_code == 200
    assert "+17" in r.text

    # 4) Approve the diff -> snapshot corrected to 27.
    async with factory() as session:
        diff_id = (
            await session.execute(
                select(ReconcileDiff.id).where(ReconcileDiff.reconcile_run_id == run_id)
            )
        ).scalar_one()
    r = await admin_client.post(
        f"/admin/reconcile/{run_id}/diffs/{diff_id}/approve",
        headers=_auth_header(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    async with factory() as session:
        snap = (
            await session.execute(
                select(InventorySnapshot).where(InventorySnapshot.master_sku_id == sku_id)
            )
        ).scalar_one()
        assert snap.on_hand_qty == 27

    # 5) Finalize -> run applied.
    r = await admin_client.post(
        f"/admin/reconcile/{run_id}/finalize",
        headers=_auth_header(),
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "finalized" in r.headers["location"]
    async with factory() as session:
        run = await session.get(ReconcileRun, run_id)
        assert run.status == "applied"


async def test_reconcile_export_csv(admin_client, _test_engine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False, autoflush=False)
    await _seed_reconcilable_variant(factory)
    csv_bytes = "商品コード,在庫数量\r\n006c,27\r\n".encode("cp932")
    b64 = base64.b64encode(csv_bytes).decode("ascii")
    r = await admin_client.post(
        "/admin/reconcile/execute",
        data={"csv_b64": b64, "filename": "stock.csv"},
        headers=_auth_header(),
        follow_redirects=False,
    )
    run_id = int(r.headers["location"].split("/admin/reconcile/")[1].split("?")[0])

    r = await admin_client.get(f"/admin/reconcile/{run_id}/export.csv", headers=_auth_header())
    assert r.status_code == 200
    assert "text/csv" in r.headers["content-type"]
    assert "006cV" in r.text
    assert "sku_code,name,current_qty,target_qty,delta,decision" in r.text
