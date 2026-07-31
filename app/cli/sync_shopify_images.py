"""Sync Shopify product images onto master SKUs (master_skus.image_url).

Walks the live Shopify catalog once and stores each variant's image URL on the
matching master SKU, so the admin screens can show a thumbnail (inventory list,
and the product preview when resolving a mapping alert).

Matching, in priority order:
1. an active channel='shopify' mapping whose channel_sku == the Shopify SKU
   (authoritative — this is the link the system actually uses), then
2. master_skus.sku_code == the Shopify SKU (post-cutover the variant masters ARE
   keyed by the real Shopify SKU).

Idempotent: re-running just refreshes URLs. Safe to schedule.

Usage (via the Cloud SQL proxy, with Shopify credentials in the environment):
    py -m app.cli.sync_shopify_images --dry-run
    py -m app.cli.sync_shopify_images
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.shopify import ShopifyAdapter
from app.config import get_settings
from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import ChannelSkuMapping, MasterSku

log = get_logger(__name__)
SessionFactory = async_sessionmaker[AsyncSession]


def build_shopify_adapter() -> ShopifyAdapter:
    settings = get_settings()
    if not settings.shopify_shop_domain or not settings.shopify_access_token:
        raise RuntimeError("Shopify credentials are missing from settings; cannot sync images.")
    return ShopifyAdapter(
        shop_domain=settings.shopify_shop_domain,
        access_token=settings.shopify_access_token,
        webhook_secret=settings.shopify_webhook_secret,
        api_version=settings.shopify_api_version,
        location_id=settings.shopify_location_id,
    )


def resolve_images(
    variants: list[dict[str, str]],
    mapping_sku_to_master: dict[str, int],
    code_to_master: dict[str, int],
) -> dict[int, str]:
    """{master_sku_id: image_url} for variants that carry an image and match a
    master. Mapping-based matches win over sku_code matches; when several
    variants of one product resolve to the same master, the first wins."""
    out: dict[int, str] = {}
    for v in variants:
        sku, url = (v.get("sku") or "").strip(), (v.get("image_url") or "").strip()
        if not sku or not url:
            continue
        master_id = mapping_sku_to_master.get(sku)
        if master_id is None:
            master_id = code_to_master.get(sku)
        if master_id is not None:
            out.setdefault(master_id, url)
    return out


async def run(*, dry_run: bool, session_factory: SessionFactory | None = None) -> int:
    adapter = build_shopify_adapter()
    async with adapter:
        variants = await adapter.list_variant_skus()
    with_image = sum(1 for v in variants if v.get("image_url"))
    log.info("sync_images.fetched", variants=len(variants), with_image=with_image)

    factory = session_factory or async_session_factory
    async with factory() as session, session.begin():
        mapping_rows = (
            await session.execute(
                select(ChannelSkuMapping.channel_sku, ChannelSkuMapping.master_sku_id).where(
                    ChannelSkuMapping.channel == "shopify",
                    ChannelSkuMapping.is_active.is_(True),
                )
            )
        ).all()
        mapping_sku_to_master = {sku: mid for sku, mid in mapping_rows}  # noqa: C416
        code_rows = (await session.execute(select(MasterSku.sku_code, MasterSku.id))).all()
        code_to_master = {code: mid for code, mid in code_rows}  # noqa: C416

        # Current DB state, so one dry-run answers "did a previous sync write?".
        total_masters = await session.scalar(select(func.count()).select_from(MasterSku)) or 0
        already_set = (
            await session.scalar(
                select(func.count()).select_from(MasterSku).where(MasterSku.image_url.isnot(None))
            )
            or 0
        )

        wanted = resolve_images(variants, mapping_sku_to_master, code_to_master)
        updated = 0
        if wanted and not dry_run:
            masters = (
                (
                    await session.execute(
                        select(MasterSku).where(MasterSku.id.in_(list(wanted.keys())))
                    )
                )
                .scalars()
                .all()
            )
            for m in masters:
                url = wanted.get(m.id)
                if url and m.image_url != url:
                    m.image_url = url
                    updated += 1
        elif wanted and dry_run:
            updated = len(wanted)

        log.info(
            "sync_images.dry_run" if dry_run else "sync_images.done",
            total_masters=total_masters,
            image_url_already_set=already_set,
            matched_masters=len(wanted),
            updated=updated,
            unmatched_variants=with_image - len(wanted),
            sample=[f"{c}" for c, _ in list(mapping_sku_to_master.items())[:3]],
        )
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Sync Shopify product images onto master SKUs")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    configure_logging("INFO")
    sys.exit(asyncio.run(run(dry_run=args.dry_run)))


if __name__ == "__main__":
    main()
