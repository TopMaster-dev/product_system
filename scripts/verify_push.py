"""Production verification script for F1.4/F1.5/F1.6 InventoryPushService.

Designed to run as a Cloud Run Job (`product-system-verify-push`) so secrets
never leave Cloud Run. Performs a single SKU push end-to-end:

  1. Resolve the adapter (rakuten | shopify) from current settings
  2. Build an InventoryPushService backed by a real DB session
  3. Call push_single() with the supplied SKU/qty
  4. Print the resulting SyncAttempt id + status to stdout
  5. Exit 0 on succeeded, 1 on failed, 2 on usage error

Per docs/13 §F1.4 the production verification uses `--quantity` equal to the
SKU's current channel-side value (no-op SET) so we exercise the code path
without changing real inventory.

Usage (locally via cloud-sql-proxy):
    py scripts/verify_push.py \\
        --channel shopify \\
        --master-sku-id 42 \\
        --channel-sku R64silverus7 \\
        --quantity 5 \\
        --triggered-by 'manual:f14-shopify-noop'

Usage (Cloud Run Job, recommended for production):
    gcloud run jobs execute product-system-verify-push \\
        --args=--channel=shopify,\\
               --master-sku-id=42,\\
               --channel-sku=R64silverus7,\\
               --quantity=5,\\
               --triggered-by=manual:f14-shopify-noop --wait
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from dataclasses import dataclass
from typing import Literal

from app.adapters.base import ChannelAdapter
from app.adapters.rakuten import RakutenAdapter
from app.adapters.shopify import ShopifyAdapter
from app.config import Settings, get_settings
from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.notifications.slack import get_slack_notifier
from app.services.inventory_push import InventoryPushService, PushRequest

log = get_logger(__name__)

Channel = Literal["shopify", "rakuten"]

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2


@dataclass(frozen=True, slots=True)
class Args:
    channel: Channel
    master_sku_id: int
    channel_sku: str
    quantity: int
    triggered_by: str


def parse_args(argv: list[str] | None = None) -> Args:
    parser = argparse.ArgumentParser(
        prog="verify_push",
        description="Push a single SKU's inventory via InventoryPushService.",
    )
    parser.add_argument(
        "--channel",
        required=True,
        choices=("shopify", "rakuten"),
        help="Target channel.",
    )
    parser.add_argument(
        "--master-sku-id",
        required=True,
        type=int,
        help="master_skus.id for the SKU being pushed.",
    )
    parser.add_argument(
        "--channel-sku",
        required=True,
        help="Channel-side SKU identifier (Shopify variant SKU or Rakuten "
        "manageNumber). Pre-resolved by the operator from channel_sku_mappings.",
    )
    parser.add_argument(
        "--quantity",
        required=True,
        type=int,
        help="Absolute on-hand quantity to SET. Use the SKU's current "
        "channel-side value for a no-op verification (docs/13 §F1.4).",
    )
    parser.add_argument(
        "--triggered-by",
        required=True,
        help="Identifier persisted in sync_attempts.payload.triggered_by. "
        "Recommended prefix: `manual:f14-` so the row is easy to find later.",
    )
    parsed = parser.parse_args(argv)
    return Args(
        channel=parsed.channel,
        master_sku_id=parsed.master_sku_id,
        channel_sku=parsed.channel_sku,
        quantity=parsed.quantity,
        triggered_by=parsed.triggered_by,
    )


def build_adapter(channel: Channel, settings: Settings) -> ChannelAdapter:
    """Instantiate the channel adapter from current Settings — no test
    doubles. The Cloud Run Job inherits the service's env, so the adapter
    points at the real Rakuten/Shopify endpoints."""
    if channel == "rakuten":
        if not settings.rakuten_service_secret or not settings.rakuten_license_key:
            raise RuntimeError("Rakuten credentials are missing from settings; cannot push.")
        return RakutenAdapter(
            service_secret=settings.rakuten_service_secret,
            license_key=settings.rakuten_license_key,
            shop_url=settings.rakuten_shop_url or None,
        )
    if channel == "shopify":
        if not settings.shopify_shop_domain or not settings.shopify_access_token:
            raise RuntimeError("Shopify credentials are missing from settings; cannot push.")
        return ShopifyAdapter(
            shop_domain=settings.shopify_shop_domain,
            access_token=settings.shopify_access_token,
            webhook_secret=settings.shopify_webhook_secret,
            api_version=settings.shopify_api_version,
            location_id=settings.shopify_location_id,
        )
    raise ValueError(f"unknown channel: {channel}")  # pragma: no cover


async def run_push(args: Args) -> int:
    settings = get_settings()
    adapter = build_adapter(args.channel, settings)
    notifier = get_slack_notifier(settings)

    async with async_session_factory() as session, session.begin():
        svc = InventoryPushService(session, notifier)
        attempt = await svc.push_single(
            adapter,
            PushRequest(
                master_sku_id=args.master_sku_id,
                channel_sku=args.channel_sku,
                quantity=args.quantity,
                triggered_by=args.triggered_by,
            ),
        )
        # Commit on success too: sync_attempts must persist even when the
        # adapter call itself succeeded, so the audit trail survives.
        attempt_id = attempt.id
        attempt_status = attempt.status
        attempt_error_code = attempt.error_code
        attempt_error_message = attempt.error_message

    # Print a single-line JSON summary to stdout so operators can grep it
    # from the Cloud Run Job log without scrolling through structlog noise.
    sys.stdout.write(
        json.dumps(
            {
                "attempt_id": attempt_id,
                "status": attempt_status,
                "channel": args.channel,
                "master_sku_id": args.master_sku_id,
                "channel_sku": args.channel_sku,
                "quantity": args.quantity,
                "error_code": attempt_error_code,
                "error_message": attempt_error_message,
            }
        )
        + "\n"
    )

    if attempt_status == "succeeded":
        return EXIT_OK
    return EXIT_FAILED


def main(argv: list[str] | None = None) -> int:
    configure_logging("INFO")
    try:
        args = parse_args(argv)
    except SystemExit as exc:  # argparse: 0 on --help, 2 on usage error
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    try:
        return asyncio.run(run_push(args))
    except Exception:
        log.exception("verify_push.unexpected_error", channel=args.channel)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
