"""Production verification script for F1.6 Shopify metadata
(location auto-discovery, inventoryItem lookup, live SKU listing, on-hand read).

Designed to run as a Cloud Run Job (`product-system-verify-shopify-meta`)
so the Shopify Admin access token never leaves Cloud Run.

All four modes are READ-ONLY — none performs the
inventorySetOnHandQuantities mutation, so they are safe to run in
production at any time.

Modes:
  - --mode=location   List the first active Shopify Location. Expects
                      exactly one row when D-2 (single-location operation)
                      holds.
  - --mode=sku        Look up the inventoryItem GID for a given SKU.
                      Exactly one match expected; zero = missing, two+ =
                      ambiguous.
  - --mode=list       List the first N live product variant SKUs (+ product
                      title). Use this to discover the real Shopify SKU
                      format when the CROSS MALL channel_sku mappings don't
                      resolve.
  - --mode=onhand     Read the current on_hand / available quantity for a
                      SKU at the primary location (the no-op SET value),
                      without a Shopify Admin login.

Exit codes:
  0 — succeeded (location/sku found, list returned, on-hand read)
  1 — verification failed (zero/multiple rows, SKU not found)
  2 — usage error

Usage (Cloud Run Job — note the script path MUST be the first --args element,
because --args replaces the container args entirely):
    gcloud run jobs execute product-system-verify-shopify-meta \\
        --args=scripts/verify_shopify_meta.py,--mode=location --wait

    gcloud run jobs execute product-system-verify-shopify-meta \\
        --args=scripts/verify_shopify_meta.py,--mode=list,--limit=30 --wait

    gcloud run jobs execute product-system-verify-shopify-meta \\
        --args=scripts/verify_shopify_meta.py,--mode=onhand,--channel-sku=R64silverus7 --wait
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

Mode = Literal["location", "sku", "list", "onhand"]

EXIT_OK = 0
EXIT_VERIFICATION_FAILED = 1
EXIT_USAGE = 2


@dataclass(frozen=True, slots=True)
class Args:
    mode: Mode
    channel_sku: str
    limit: int


def parse_args(argv: list[str] | None = None) -> Args:
    parser = argparse.ArgumentParser(
        prog="verify_shopify_meta",
        description="Read-only checks of Shopify Location / SKU / variant-list / on-hand paths.",
    )
    parser.add_argument(
        "--mode",
        required=True,
        choices=("location", "sku", "list", "onhand"),
        help="Which read-only check to run.",
    )
    parser.add_argument(
        "--channel-sku",
        default="",
        help="SKU to look up. Required with --mode=sku and --mode=onhand.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="How many variant SKUs to list (--mode=list). Default 20.",
    )
    parsed = parser.parse_args(argv)
    if parsed.mode in ("sku", "onhand") and not parsed.channel_sku:
        parser.error(f"--mode={parsed.mode} requires --channel-sku")
    return Args(mode=parsed.mode, channel_sku=parsed.channel_sku, limit=parsed.limit)


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


async def verify_list(limit: int) -> int:
    adapter = build_adapter()
    async with adapter:
        variants = await adapter.list_variant_skus(first=limit)
    sys.stdout.write(
        json.dumps({"mode": "list", "result": "ok", "count": len(variants), "variants": variants})
        + "\n"
    )
    return EXIT_OK


async def verify_onhand(channel_sku: str) -> int:
    adapter = build_adapter()
    async with adapter:
        try:
            quantities = await adapter.get_on_hand(channel_sku)
        except RuntimeError as exc:
            sys.stdout.write(
                json.dumps(
                    {
                        "mode": "onhand",
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
                "mode": "onhand",
                "channel_sku": channel_sku,
                "result": "ok",
                "quantities": quantities,
            }
        )
        + "\n"
    )
    return EXIT_OK


async def run(args: Args) -> int:
    if args.mode == "location":
        return await verify_location()
    if args.mode == "list":
        return await verify_list(args.limit)
    if args.mode == "onhand":
        return await verify_onhand(args.channel_sku)
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
