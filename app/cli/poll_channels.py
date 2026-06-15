"""Channel polling entrypoint — fetches Shopify and Rakuten orders since the
last successful poll and ingests them.

    py -m app.cli.poll_channels                    # both channels
    py -m app.cli.poll_channels --channel rakuten  # one channel

Suitable to be triggered by Cloud Scheduler every 5-15 minutes. Polling
watermarks live in `bigquery_export_runs` analog could be added later; for
Phase 1-A we use a simple "last <minutes>" lookback to keep it stateless.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta

from app.adapters import RakutenAdapter, ShopifyAdapter
from app.config import get_settings
from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.services import OrderIngestService

log = get_logger(__name__)


async def _poll_shopify(lookback_minutes: int) -> int:
    settings = get_settings()
    since = datetime.now(UTC) - timedelta(minutes=lookback_minutes)
    async with ShopifyAdapter(
        shop_domain=settings.shopify_shop_domain,
        access_token=settings.shopify_access_token,
        webhook_secret=settings.shopify_webhook_secret,
        api_version=settings.shopify_api_version,
        location_id=settings.shopify_location_id,
    ) as adapter:
        orders = await adapter.fetch_orders(since=since)
    return await _ingest(orders)


async def _poll_rakuten(lookback_minutes: int) -> int:
    settings = get_settings()
    since = datetime.now(UTC) - timedelta(minutes=lookback_minutes)
    async with RakutenAdapter(
        service_secret=settings.rakuten_service_secret,
        license_key=settings.rakuten_license_key,
        shop_url=settings.rakuten_shop_url,
    ) as adapter:
        orders = await adapter.fetch_orders(since=since)
    return await _ingest(orders)


async def _ingest(orders) -> int:  # type: ignore[no-untyped-def]
    count = 0
    async with async_session_factory() as session, session.begin():
        ingest = OrderIngestService(session)
        for order in orders:
            await ingest.ingest(order)
            count += 1
    return count


async def run(channel: str, lookback_minutes: int) -> int:
    if channel in {"shopify", "all"}:
        n = await _poll_shopify(lookback_minutes)
        log.info("poll.shopify.done", ingested=n)
    if channel in {"rakuten", "all"}:
        n = await _poll_rakuten(lookback_minutes)
        log.info("poll.rakuten.done", ingested=n)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Poll channels for new orders")
    parser.add_argument("--channel", choices=("all", "shopify", "rakuten"), default="all")
    parser.add_argument("--lookback-minutes", type=int, default=20)
    args = parser.parse_args()
    configure_logging(get_settings().app_log_level)
    sys.exit(asyncio.run(run(args.channel, args.lookback_minutes)))


if __name__ == "__main__":
    main()
