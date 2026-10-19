"""RakutenAdapter の在庫書き込み — 在庫API 2.0 (P2-039).

Rewritten 2026-10-19 against the client's own RMS spec. The previous version of
this file asserted a request shape that was never a Rakuten route at all: it
pinned `/es/2.0/inventory/updateInventory/`, which answers 404 from Apache, so
every assertion passed against a MockTransport while production had never once
succeeded. Tests that pin a shape nobody checked are worse than no tests, and
that is the specific failure these replace.

What the spec says, and what is therefore asserted here:
  * success is HTTP 204 with NO body;
  * stock is keyed on the PAIR (商品管理番号, SKU管理番号);
  * `quantity` is a JSON number, `mode` is ABSOLUTE;
  * errors arrive as an `errors` array, not as a 200 carrying an error code.
"""

from __future__ import annotations

import json

import httpx
import pytest

from app.adapters import rakuten_bulk_upsert as bulk
from app.adapters.rakuten import RakutenAdapter, RakutenTransientError
from app.adapters.rate_limit import TokenBucket

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _adapter(handler) -> tuple[RakutenAdapter, list[httpx.Request]]:
    captured: list[httpx.Request] = []

    def _wrap(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return handler(req)

    client = httpx.AsyncClient(transport=httpx.MockTransport(_wrap))
    adapter = RakutenAdapter(
        service_secret="ss",
        license_key="lk",
        client=client,
        rate_limiter=TokenBucket(rate=100, capacity=1000),
    )
    return adapter, captured


def _ok(_: httpx.Request) -> httpx.Response:
    """Rakuten's success: 204, no body."""
    return httpx.Response(204)


# --- 単品の書き込み ----------------------------------------------------------


async def test_a_single_sku_is_written_with_the_pair_in_the_url() -> None:
    adapter, captured = _adapter(_ok)
    async with adapter:
        result = await adapter.push_inventory("073", 42, variant_key="1435")

    assert result is None  # 204 carries nothing to record
    assert captured[0].method == "PUT"
    assert captured[0].url.path == "/es/2.0/inventories/manage-numbers/073/variants/1435"
    body = json.loads(captured[0].content)
    assert body == {"mode": "ABSOLUTE", "quantity": 42}


async def test_a_push_without_the_sku_key_is_refused() -> None:
    """This shop sells 項目選択肢別在庫 only. A product-level write has nowhere to
    land, and guessing would publish one size's count onto every size."""
    adapter, captured = _adapter(_ok)
    async with adapter:
        with pytest.raises(ValueError, match="SKU管理番号"):
            await adapter.push_inventory("073", 42)

    assert captured == []


async def test_a_negative_quantity_never_reaches_the_network() -> None:
    adapter, captured = _adapter(_ok)
    async with adapter:
        with pytest.raises(bulk.RowRejected, match="負の在庫数"):
            await adapter.push_inventory("073", -1, variant_key="1435")

    assert captured == []


async def test_the_auth_header_is_esa_and_carries_no_newline() -> None:
    """Re-tests the Phase 1-A CR/LF fix on the path that now does the writing:
    a credential copied from a Windows source carries a trailing \\r, which
    corrupts the base64 token and returns ES04-01 on every call."""
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(204)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = RakutenAdapter(
        service_secret="ss\r",
        license_key="lk\n",
        client=client,
        rate_limiter=TokenBucket(rate=100, capacity=1000),
    )
    async with adapter:
        await adapter.push_inventory("073", 1, variant_key="1435")

    auth = captured[0].headers.get("authorization") or ""
    assert auth.startswith("ESA ")
    assert "\r" not in auth
    assert "\n" not in auth


# --- 一括の書き込み ----------------------------------------------------------


async def test_a_batch_goes_in_one_request() -> None:
    """The rate limit is per request, so this is the difference between two
    calls and several hundred seconds."""
    adapter, captured = _adapter(_ok)
    rows = [bulk.InventoryRow("073", str(i), i) for i in range(1, 51)]
    async with adapter:
        await adapter.push_inventory_batch(rows)

    assert len(captured) == 1
    assert captured[0].method == "POST"
    assert captured[0].url.path == "/es/2.0/inventories/bulk-upsert"
    sent = json.loads(captured[0].content)["inventories"]
    assert len(sent) == 50
    assert sent[0] == {"manageNumber": "073", "variantId": "1", "mode": "ABSOLUTE", "quantity": 1}


async def test_more_than_four_hundred_rows_split_into_several_requests() -> None:
    adapter, captured = _adapter(_ok)
    rows = [bulk.InventoryRow("073", str(i), 1) for i in range(401)]
    async with adapter:
        await adapter.push_inventory_batch(rows)

    assert [len(json.loads(r.content)["inventories"]) for r in captured] == [400, 1]


# --- 失敗の読み取り ----------------------------------------------------------


async def test_a_rejected_write_names_the_error_and_the_unverified_rows() -> None:
    """204 carries no per-row status, so a rejection leaves every row in the
    request unverified — the message has to say so."""

    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={
                "errors": [
                    {
                        "code": "INVALID_VALUE",
                        "message": "quantity is invalid",
                        "metadata": {"propertyPath": "inventories[0].quantity"},
                    }
                ]
            },
        )

    adapter, _ = _adapter(handler)
    async with adapter:
        with pytest.raises(RuntimeError) as caught:
            await adapter.push_inventory("073", 1, variant_key="1435")

    message = str(caught.value)
    assert "INVALID_VALUE" in message
    assert "inventories[0].quantity" in message
    assert "1 row(s) must be re-read" in message


async def test_a_permanent_rejection_is_not_retried() -> None:
    """A 400 is a verdict about the data. Retrying it five times only repeats a
    rejected write and delays the error."""
    adapter, captured = _adapter(lambda _: httpx.Response(400, json={"errors": []}))
    async with adapter:
        with pytest.raises(RuntimeError):
            await adapter.push_inventory("073", 1, variant_key="1435")

    assert len(captured) == 1


async def test_a_server_error_is_retried_because_absolute_writes_are_idempotent() -> None:
    attempts: list[int] = []

    def handler(_: httpx.Request) -> httpx.Response:
        attempts.append(1)
        return httpx.Response(204) if len(attempts) >= 3 else httpx.Response(503)

    adapter, _ = _adapter(handler)
    async with adapter:
        await adapter.push_inventory("073", 7, variant_key="1435")

    assert len(attempts) == 3


async def test_a_success_is_never_reported_as_a_failure() -> None:
    """The regression this guards: the old transport ended in `resp.json()`,
    which raises on an empty 204 body. JSONDecodeError is not an httpx error, so
    it escaped the retry and would have recorded every successful write as
    failed, with a Slack error each time."""
    adapter, _ = _adapter(lambda _: httpx.Response(204, content=b""))
    async with adapter:
        assert await adapter.push_inventory("073", 3, variant_key="1435") is None


async def test_an_unexpected_success_code_is_not_treated_as_success() -> None:
    """Success is 204 and nothing else. A 200 means Rakuten answered in a way
    this code was not written against."""
    adapter, _ = _adapter(lambda _: httpx.Response(200, json={"ok": True}))
    async with adapter:
        with pytest.raises(RuntimeError):
            await adapter.push_inventory("073", 1, variant_key="1435")


async def test_a_transient_failure_that_never_clears_is_raised() -> None:
    adapter, captured = _adapter(lambda _: httpx.Response(503))
    async with adapter:
        with pytest.raises(RakutenTransientError):
            await adapter.push_inventory("073", 1, variant_key="1435")

    assert len(captured) == 5
