"""Backfill mapping_alerts.product_name for alerts created before product-name
capture existed.

Existing unmapped-SKU alerts show only a bare channel SKU; this reads the
originating order's stored `raw_payload` and fills the channel's own product
name (楽天=itemName / Shopify=line name) so operators can identify the product.

Self-contained payload parsing (not the adapters) so it stays robust on old /
partial payloads. Idempotent: only fills alerts whose product_name IS NULL.

Usage (via the Cloud SQL proxy):
    py -m app.cli.backfill_alert_product_names --dry-run
    py -m app.cli.backfill_alert_product_names
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import MappingAlert, MappingAlertStatusEnum, Order

log = get_logger(__name__)
SessionFactory = async_sessionmaker[AsyncSession]

_OUTSTANDING = [MappingAlertStatusEnum.OPEN.value, MappingAlertStatusEnum.IN_PROGRESS.value]


def extract_names(channel: str, raw: dict[str, Any] | None) -> dict[str, str]:
    """{channel_sku: product_name} from a stored order raw_payload."""
    out: dict[str, str] = {}
    if not raw:
        return out
    try:
        if channel == "rakuten":
            for pkg in raw.get("PackageModelList") or []:
                for ln in pkg.get("ItemModelList") or []:
                    sku = str(ln.get("manageNumber") or ln.get("itemNumber") or "")
                    name = str(ln.get("itemName") or "")
                    if sku and name:
                        out.setdefault(sku, name)
        elif channel == "shopify":
            for edge in (raw.get("lineItems") or {}).get("edges", []):  # GraphQL shape
                ln = edge.get("node") or {}
                sku, name = ln.get("sku") or "", ln.get("name") or ""
                if sku and name:
                    out.setdefault(sku, name)
            for ln in raw.get("line_items") or []:  # REST/webhook shape
                sku = ln.get("sku") or ""
                name = ln.get("name") or ln.get("title") or ""
                if sku and name:
                    out.setdefault(sku, name)
    except Exception:  # pragma: no cover - defensive on unexpected payload shapes
        log.warning("backfill_alert_names.parse_failed", channel=channel)
    return out


async def run(*, dry_run: bool, session_factory: SessionFactory | None = None) -> int:
    factory = session_factory or async_session_factory
    async with factory() as session, session.begin():
        alerts = list(
            (
                await session.execute(
                    select(MappingAlert).where(
                        MappingAlert.product_name.is_(None),
                        MappingAlert.status.in_(_OUTSTANDING),
                    )
                )
            )
            .scalars()
            .all()
        )
        if not alerts:
            log.info("backfill_alert_names.none")
            return 0

        want: dict[tuple[str, str], MappingAlert] = {(a.channel, a.channel_sku): a for a in alerts}
        channels = {a.channel for a in alerts}
        orders = (
            await session.execute(
                select(Order.channel, Order.raw_payload).where(
                    Order.channel.in_(channels),
                    Order.raw_payload.isnot(None),
                )
            )
        ).all()

        filled = 0
        for channel, raw in orders:
            if not want:
                break
            for csku, name in extract_names(channel, raw).items():
                alert = want.get((channel, csku))
                if alert is not None:
                    if not dry_run:
                        alert.product_name = name
                    want.pop((channel, csku), None)
                    filled += 1

        log.info(
            "backfill_alert_names.dry_run" if dry_run else "backfill_alert_names.done",
            candidates=len(alerts),
            filled=filled,
            still_missing=len(want),
        )
    return 0


def main() -> None:
    p = argparse.ArgumentParser(
        description="Backfill mapping_alerts.product_name from order payloads"
    )
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    configure_logging("INFO")
    sys.exit(asyncio.run(run(dry_run=args.dry_run)))


if __name__ == "__main__":
    main()
