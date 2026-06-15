"""Unit tests for scripts/verify_shopify_meta.py."""

from __future__ import annotations

import io
import sys
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from verify_shopify_meta import (  # noqa: E402
    EXIT_OK,
    EXIT_VERIFICATION_FAILED,
    Args,
    main,
    parse_args,
)


class _FakeSettings:
    shopify_shop_domain = "verify.myshopify.com"
    shopify_access_token = "tok"
    shopify_webhook_secret = "wh"
    shopify_api_version = "2025-04"


# ---------- argparse ----------


@pytest.mark.unit
def test_parse_args_location_mode() -> None:
    args = parse_args(["--mode", "location"])
    assert args == Args(mode="location", channel_sku="")


@pytest.mark.unit
def test_parse_args_sku_mode_requires_channel_sku() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--mode", "sku"])


@pytest.mark.unit
def test_parse_args_sku_mode_with_value() -> None:
    args = parse_args(["--mode", "sku", "--channel-sku", "R64silverus7"])
    assert args.mode == "sku"
    assert args.channel_sku == "R64silverus7"


@pytest.mark.unit
def test_parse_args_rejects_unknown_mode() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--mode", "bogus"])


# ---------- main() with mocked GraphQL ----------


def _adapter_with(handler):  # type: ignore[no-untyped-def]
    """Build a ShopifyAdapter wrapping a MockTransport handler. Returns
    (adapter, captured_requests). Used inside the verify_shopify_meta
    module's build_adapter() patch site."""
    captured: list[httpx.Request] = []

    def _wrap(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return handler(req)

    transport = httpx.MockTransport(_wrap)
    client = httpx.AsyncClient(transport=transport)

    from app.adapters.rate_limit import TokenBucket
    from app.adapters.shopify import ShopifyAdapter

    adapter = ShopifyAdapter(
        shop_domain="verify.myshopify.com",
        access_token="tok",
        webhook_secret="wh",
        api_version="2025-04",
        location_id="",
        client=client,
        rate_limiter=TokenBucket(rate=100, capacity=1000),
    )
    return adapter, captured


@pytest.mark.unit
def test_main_location_mode_returns_0_on_single_location(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "locations": {
                        "edges": [{"node": {"id": "gid://shopify/Location/9", "name": "Main"}}]
                    }
                }
            },
        )

    adapter, _captured = _adapter_with(handler)
    with patch("verify_shopify_meta.build_adapter", return_value=adapter):
        code = main(["--mode", "location"])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert '"result": "ok"' in out
    assert "gid://shopify/Location/9" in out


@pytest.mark.unit
def test_main_location_mode_returns_1_on_no_locations(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"locations": {"edges": []}}})

    adapter, _ = _adapter_with(handler)
    with patch("verify_shopify_meta.build_adapter", return_value=adapter):
        code = main(["--mode", "location"])
    assert code == EXIT_VERIFICATION_FAILED
    out = capsys.readouterr().out
    assert '"result": "error"' in out


@pytest.mark.unit
def test_main_sku_mode_returns_0_on_single_match(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "inventoryItems": {
                        "edges": [
                            {
                                "node": {
                                    "id": "gid://shopify/InventoryItem/777",
                                    "sku": "ABC",
                                }
                            }
                        ]
                    }
                }
            },
        )

    adapter, _ = _adapter_with(handler)
    with patch("verify_shopify_meta.build_adapter", return_value=adapter):
        code = main(["--mode", "sku", "--channel-sku", "ABC"])
    assert code == EXIT_OK
    out = capsys.readouterr().out
    assert "gid://shopify/InventoryItem/777" in out


@pytest.mark.unit
def test_main_sku_mode_returns_1_on_no_match(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": {"inventoryItems": {"edges": []}}})

    adapter, _ = _adapter_with(handler)
    with patch("verify_shopify_meta.build_adapter", return_value=adapter):
        code = main(["--mode", "sku", "--channel-sku", "MISSING"])
    assert code == EXIT_VERIFICATION_FAILED


@pytest.mark.unit
def test_main_sku_mode_returns_1_on_ambiguous(
    capsys: pytest.CaptureFixture[str],
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "data": {
                    "inventoryItems": {
                        "edges": [
                            {"node": {"id": "gid://shopify/InventoryItem/1", "sku": "D"}},
                            {"node": {"id": "gid://shopify/InventoryItem/2", "sku": "D"}},
                        ]
                    }
                }
            },
        )

    adapter, _ = _adapter_with(handler)
    with patch("verify_shopify_meta.build_adapter", return_value=adapter):
        code = main(["--mode", "sku", "--channel-sku", "D"])
    assert code == EXIT_VERIFICATION_FAILED


@pytest.mark.unit
def test_main_usage_error_returns_2() -> None:
    saved = sys.stderr
    try:
        sys.stderr = io.StringIO()
        code = main(["--mode", "sku"])  # missing --channel-sku
    finally:
        sys.stderr = saved
    assert code == 2
