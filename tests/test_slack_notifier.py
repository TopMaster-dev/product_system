"""Unit tests for the Slack notifier.

These tests use httpx.MockTransport — no network access. Covers:
- no-op when webhook URL is empty (per D-3 initial state)
- level filtering (min_level default 'error' suppresses info)
- successful delivery returns True and produces the expected payload
- HTTP non-2xx returns False and does not raise
- transport errors return False and do not raise
- attachment structure (color, title prefix, fields shape)
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.notifications.slack import SlackNotifier


@pytest.mark.unit
@pytest.mark.asyncio
async def test_no_url_is_noop() -> None:
    n = SlackNotifier(webhook_url="", min_level="error")
    assert n.is_enabled is False
    sent = await n.notify(level="error", title="x", message="y")
    assert sent is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_below_min_level_skipped() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, text="ok")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        n = SlackNotifier(
            webhook_url="https://hooks.slack.test/T/B/X",
            min_level="error",
            client=client,
        )
        sent = await n.notify(level="info", title="x", message="y")
    assert sent is False
    assert captured == []


@pytest.mark.unit
@pytest.mark.asyncio
async def test_successful_post() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, text="ok")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        n = SlackNotifier(
            webhook_url="https://hooks.slack.test/T/B/X",
            min_level="error",
            client=client,
        )
        sent = await n.notify(
            level="critical",
            title="DB down",
            message="cannot reach postgres",
            fields=[("env", "prod"), ("sku_count", "0")],
        )

    assert sent is True
    assert len(captured) == 1
    body = json.loads(captured[0].content)
    assert "attachments" in body
    att = body["attachments"][0]
    assert att["title"].startswith("[CRITICAL]")
    assert att["color"] == "#dc2626"
    assert any(f["title"] == "env" and f["value"] == "prod" for f in att["fields"])


@pytest.mark.unit
@pytest.mark.asyncio
async def test_non_2xx_returns_false_no_raise() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        n = SlackNotifier(
            webhook_url="https://hooks.slack.test/T/B/X",
            min_level="error",
            client=client,
        )
        sent = await n.notify(level="error", title="x", message="y")
    assert sent is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_transport_error_returns_false_no_raise() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns failed")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        n = SlackNotifier(
            webhook_url="https://hooks.slack.test/T/B/X",
            min_level="error",
            client=client,
        )
        sent = await n.notify(level="error", title="x", message="y")
    assert sent is False


@pytest.mark.unit
@pytest.mark.asyncio
async def test_attachment_color_by_level() -> None:
    captured: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(json.loads(request.content))
        return httpx.Response(200, text="ok")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        for level, expected_color in [
            ("critical", "#dc2626"),
            ("error", "#f59e0b"),
        ]:
            n = SlackNotifier(
                webhook_url="https://hooks.slack.test/T/B/X",
                min_level="error",
                client=client,
            )
            await n.notify(level=level, title="t", message="m")  # type: ignore[arg-type]
    assert [c["attachments"][0]["color"] for c in captured] == ["#dc2626", "#f59e0b"]
