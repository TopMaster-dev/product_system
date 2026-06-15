"""Unit tests for InventoryPushService.

Uses a fake async session that records `add` / `flush` calls and assigns
ids on flush — no Postgres required. Adapter is a stub.
"""

from __future__ import annotations

import httpx
import pytest

from app.adapters.base import ChannelAdapter
from app.models import SyncAttempt, SyncAttemptStatusEnum
from app.notifications.slack import SlackNotifier
from app.services.inventory_push import InventoryPushService, PushRequest


class _FakeSession:
    """Minimal in-memory stand-in for AsyncSession.

    Records add/flush so the test can inspect them; assigns id on flush so
    `attempt.id` is observable, matching real ORM behavior.
    """

    def __init__(self) -> None:
        self._next_id = 1
        self.added: list[SyncAttempt] = []
        self.flush_calls = 0

    def add(self, obj: SyncAttempt) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flush_calls += 1
        for o in self.added:
            if o.id is None:
                o.id = self._next_id
                self._next_id += 1


class _StubAdapter(ChannelAdapter):
    channel = "shopify"

    def __init__(self, *, return_value=None, raise_exc: BaseException | None = None) -> None:
        self._return_value = return_value
        self._raise = raise_exc
        self.calls: list[tuple[str, int]] = []

    async def fetch_orders(self, since, until=None):  # pragma: no cover
        raise NotImplementedError

    async def push_inventory(self, sku: str, quantity: int):
        self.calls.append((sku, quantity))
        if self._raise is not None:
            raise self._raise
        return self._return_value

    def verify_webhook(self, headers, body):  # pragma: no cover
        return True


def _no_op_notifier() -> SlackNotifier:
    return SlackNotifier(webhook_url="", min_level="error")


@pytest.mark.unit
@pytest.mark.asyncio
async def test_successful_push_records_succeeded_attempt() -> None:
    session = _FakeSession()
    adapter = _StubAdapter(return_value={"inventory_item_id": "abc"})
    svc = InventoryPushService(session, _no_op_notifier())  # type: ignore[arg-type]

    attempt = await svc.push_single(
        adapter,
        PushRequest(
            master_sku_id=10,
            channel_sku="SKU-001",
            quantity=7,
            triggered_by="reconcile_run:42",
        ),
    )

    assert adapter.calls == [("SKU-001", 7)]
    assert len(session.added) == 1
    assert attempt.status == SyncAttemptStatusEnum.SUCCEEDED.value
    assert attempt.error_code is None
    assert attempt.finished_at is not None
    assert attempt.response_payload == {"inventory_item_id": "abc"}
    # 1 flush after insert (pending), 1 after marking succeeded
    assert session.flush_calls == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_pending_state_persisted_before_call() -> None:
    """If the adapter raises, the attempt row must exist (status=failed)
    rather than be lost — proves the pending row was flushed up front."""
    session = _FakeSession()
    adapter = _StubAdapter(raise_exc=RuntimeError("api down"))
    svc = InventoryPushService(session, _no_op_notifier())

    attempt = await svc.push_single(
        adapter,
        PushRequest(master_sku_id=1, channel_sku="X", quantity=1, triggered_by="t"),
    )

    assert attempt.id is not None  # got an id from the initial flush
    assert attempt.status == SyncAttemptStatusEnum.FAILED.value
    assert attempt.error_code == "RuntimeError"
    assert attempt.error_message == "api down"
    assert attempt.finished_at is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_failure_notifies_slack() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, text="ok")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        notifier = SlackNotifier(
            webhook_url="https://hooks.slack.test/T/B/X",
            min_level="error",
            client=client,
        )
        session = _FakeSession()
        adapter = _StubAdapter(
            raise_exc=httpx.HTTPStatusError(
                "429 too many requests",
                request=httpx.Request("POST", "https://x"),
                response=httpx.Response(429),
            )
        )
        svc = InventoryPushService(session, notifier)
        await svc.push_single(
            adapter,
            PushRequest(master_sku_id=3, channel_sku="B23gold", quantity=2, triggered_by="t"),
        )

    assert len(captured) == 1
    import json

    body = json.loads(captured[0].content)
    att = body["attachments"][0]
    assert att["title"].startswith("[ERROR]")
    assert "B23gold" in str(att["fields"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_success_does_not_notify() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, text="ok")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        notifier = SlackNotifier(
            webhook_url="https://hooks.slack.test/T/B/X",
            min_level="error",
            client=client,
        )
        session = _FakeSession()
        adapter = _StubAdapter(return_value=None)
        svc = InventoryPushService(session, notifier)
        await svc.push_single(
            adapter,
            PushRequest(master_sku_id=3, channel_sku="x", quantity=2, triggered_by="t"),
        )
    assert captured == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_attempt_payload_records_request_inputs() -> None:
    session = _FakeSession()
    adapter = _StubAdapter()
    svc = InventoryPushService(session, _no_op_notifier())
    attempt = await svc.push_single(
        adapter,
        PushRequest(
            master_sku_id=5,
            channel_sku="N111gold",
            quantity=42,
            triggered_by="reconcile_run:99",
            parent_attempt_id=11,
        ),
    )
    assert attempt.payload["channel_sku"] == "N111gold"
    assert attempt.payload["quantity"] == 42
    assert attempt.payload["triggered_by"] == "reconcile_run:99"
    assert attempt.parent_attempt_id == 11


@pytest.mark.unit
@pytest.mark.asyncio
async def test_error_message_truncated_to_500_chars() -> None:
    session = _FakeSession()
    adapter = _StubAdapter(raise_exc=RuntimeError("x" * 1000))
    svc = InventoryPushService(session, _no_op_notifier())
    attempt = await svc.push_single(
        adapter,
        PushRequest(master_sku_id=1, channel_sku="X", quantity=1, triggered_by="t"),
    )
    assert attempt.error_message is not None
    assert len(attempt.error_message) <= 500


@pytest.mark.unit
@pytest.mark.asyncio
async def test_empty_error_message_falls_back_to_class_name() -> None:
    class _Bare(Exception):
        pass

    session = _FakeSession()
    adapter = _StubAdapter(raise_exc=_Bare())
    svc = InventoryPushService(session, _no_op_notifier())
    attempt = await svc.push_single(
        adapter,
        PushRequest(master_sku_id=1, channel_sku="X", quantity=1, triggered_by="t"),
    )
    assert attempt.error_message == "_Bare"
