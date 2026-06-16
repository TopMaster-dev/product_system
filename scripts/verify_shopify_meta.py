"""Production verification script for F1.6 Shopify metadata
(primary location auto-discovery + inventoryItem lookup by SKU).

Designed to run as a Cloud Run Job (`product-system-verify-shopify-meta`)
so the Shopify Admin access token never leaves Cloud Run.

This script verifies the two read-only GraphQL operations that the
Shopify push path depends on, WITHOUT performing the
inventorySetOnHandQuantities mutation. That makes it safe to run in
production at any time — no inventory is modified.

Two modes:
  - --mode=location   List the first active Shopify Location. Expects
                      exactly one row when D-2 (single-location operation)
                      holds. Multiple rows means SHOPIFY_LOCATION_ID must
                      be configured explicitly.
  - --mode=sku        Look up the inventoryItem GID for a given SKU.
                      Expects exactly one match — zero means a missing
                      product, two+ means the SKU is ambiguous in the shop.

Exit codes:
  0 — single expected row found
  1 — zero or multiple rows (verification failed)
  2 — usage error

Usage (Cloud Run Job):
    gcloud run jobs execute product-system-verify-shopify-meta \\
        --args=--mode=location --wait

    gcloud run jobs execute product-system-verify-shopify-meta \\
        --args=--mode=sku,--channel-sku=R64silverus7 --wait
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from typing import Literal

from app.adapters.shopify import ShopifyAdapter
from app.config import get_settings
from app.logging import configure_logging, get_logger

log = get_logger(__name__)

Mode = Literal["location", "sku"]

EXIT_OK = 0
EXIT_VERIFICATION_FAILED = 1
EXIT_USAGE = 2


@dataclass(frozen=True, slots=True)
class Args:
    mode: Mode
    channel_sku: str


def parse_args(argv: list[str] | None = None) -> Args:
    parser = argparse.ArgumentParser(
        prog="verify_shopify_meta",
        description="Read-only check of Shopify Location and SKU lookup paths.",
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=("location", "sku"),
        help="Which read-only check to run.",
    )
    parser.add_argument(
        "--channel-sku",
        default="",
        help="SKU to look up. Required only with --mode=sku.",
    )
    parsed = parser.parse_args(argv)
    if parsed.mode == "sku" and not parsed.channel_sku:
        parser.error("--mode=sku requires --channel-sku")
    return Args(mode=parsed.mode, channel_sku=parsed.channel_sku)


def build_adapter() -> ShopifyAdapter:
    """Build a fresh adapter from settings so no cached location_id is
    used (we want to exercise auto-discovery on demand)."""
    settings = get_settings()
    if not settings.shopify_shop_domain or not settings.shopify_access_token:
        raise RuntimeError("Shopify credentials are missing from settings; cannot verify.")
    return ShopifyAdapter(
        shop_domain=settings.shopify_shop_domain,
        access_token=settings.shopify_access_token,
        webhook_secret=settings.shopify_webhook_secret,
        api_version=settings.shopify_api_version,
        location_id="",  # force discovery
    )


async def verify_location() -> int:
    adapter = build_adapter()
    async with adapter:
        try:
            loc = await adapter._resolve_location_id()  # type: ignore[attr-defined]
        except RuntimeError as exc:
            sys.stdout.write(
                json.dumps({"mode": "location", "result": "error", "error": str(exc)}) + "\n"
            )
            return EXIT_VERIFICATION_FAILED
    sys.stdout.write(json.dumps({"mode": "location", "result": "ok", "location_id": loc}) + "\n")
    return EXIT_OK


async def verify_sku(channel_sku: str) -> int:
    adapter = build_adapter()
    async with adapter:
        try:
            item_id = await adapter._lookup_inventory_item_id(channel_sku)  # type: ignore[attr-defined]
        except RuntimeError as exc:
            sys.stdout.write(
                json.dumps(
                    {
                        "mode": "sku",
                        "channel_sku": channel_sku,
                        "result": "error",
                        "error": str(exc),
                    }
                )
                + "\n"
            )
            return EXIT_VERIFICATION_FAILED
    sys.stdout.write(
        json.dumps(
            {
                "mode": "sku",
                "channel_sku": channel_sku,
                "result": "ok",
                "inventory_item_id": item_id,
            }
        )
        + "\n"
    )
    return EXIT_OK


async def run(args: Args) -> int:
    if args.mode == "location":
        return await verify_location()
    return await verify_sku(args.channel_sku)


def main(argv: list[str] | None = None) -> int:
    configure_logging("INFO")
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    try:
        return asyncio.run(run(args))
    except Exception:
        log.exception("verify_shopify_meta.unexpected_error", mode=args.mode)
        return EXIT_VERIFICATION_FAILED


if __name__ == "__main__":
    sys.exit(main())
