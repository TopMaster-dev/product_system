"""Discover new Shopify products and variants and register them (P2-034).

CROSS MALL has supplied the product master until now. When it shuts down
(client: early October 2026) nothing replaces it, and a new product or a new
colour/size would exist only in Shopify. That gap is not hypothetical: N128 and
B73 are absent from the master entirely while having sold 123 units across 78
orders, and the N108 length variants are unmodelled across another 72 orders.
Every one of those lines arrives as an unmapped sale.

This reads the Shopify catalogue and closes that gap in three ways per variant:

  already mapped  nothing to do
  master exists   the master is there under this sku_code but nothing links it
                  to Shopify — add the mapping only, never a second master
  new             neither exists — create the master and the mapping

THREE RULES THAT MATTER

1. It only ever ADDS. It never archives, deactivates or deletes a master that
   Shopify does not return. Roughly half the catalogue sells on Rakuten, and a
   master absent from Shopify is usually a Rakuten-only product, not a retired
   one. "Tidying up" those would deactivate live mappings and strand real stock.

2. It never sets stock. A new master starts with no snapshot at all, and stock
   arrives from 棚卸 or 入荷. Seeding from Shopify's numbers would import as
   truth exactly the figures the stock audit (P2-035) exists to CHECK.

3. It reports by default and writes only with --apply. Every other CLI here
   takes --dry-run instead; this one is inverted deliberately, because it
   creates master rows and the no-flag invocation is the one an operator reaches
   for first.

Usage (via the Cloud SQL proxy, Shopify credentials required):
    powershell -File scripts/run_cli.ps1 -Cli sync_shopify_masters -WithShopify
    powershell -File scripts/run_cli.ps1 -Cli sync_shopify_masters -WithShopify -Apply
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from collections import Counter
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.cli._adapters import build_shopify_adapter
from app.cli.build_channel_mapping import extract_token
from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import ChannelSkuMapping, MasterSku

log = get_logger(__name__)
SessionFactory = async_sessionmaker[AsyncSession]

CHANNEL = "shopify"

#: Shopify option names are free text, so the shop can call the colour axis
#: whatever it likes. These are the spellings actually in use; anything else is
#: preserved verbatim in `attributes` rather than guessed at.
COLOUR_OPTION_NAMES = frozenset({"色", "カラー", "color", "colour"})
SIZE_OPTION_NAMES = frozenset({"サイズ", "size", "長さ", "length"})

#: Shopify's placeholder for a product with no real options. It is not a size.
DEFAULT_OPTION_VALUE = "Default Title"


@dataclass(frozen=True, slots=True)
class Discovery:
    """One Shopify variant that the master does not yet account for."""

    sku: str
    name: str
    colour: str
    size: str
    token: str | None
    image_url: str
    #: Set when a master already carries this sku_code — then only the mapping is
    #: missing, and creating a second master would be the wrong repair.
    existing_master_id: int | None
    #: Existing masters sharing this variant's product token. A new "N108gold42"
    #: next to an existing "N108gold" is usually another LENGTH of the same
    #: physical item, not a new product — so it probably belongs in a shared
    #: stock pool rather than as an independent master holding its own stock.
    #: Reported rather than acted on: only the client knows which it is.
    related: tuple[str, ...] = ()


def classify_options(options: dict[str, str]) -> tuple[str, str]:
    """Shopify's selectedOptions -> (colour, size).

    Matching is on the option NAME, not on the value, so a size called "22cm"
    and a colour called "gold" cannot be swapped by a value that looks like the
    other. Unrecognised axes return blank rather than being forced into one of
    the two slots: a third axis exists in this catalogue (ring US sizes sit
    alongside colour) and mislabelling it would produce a master whose
    attributes disagree with its SKU.
    """
    colour = size = ""
    for name, value in options.items():
        key = name.strip().lower()
        if value.strip() == DEFAULT_OPTION_VALUE:
            continue
        if key in COLOUR_OPTION_NAMES:
            colour = value.strip()
        elif key in SIZE_OPTION_NAMES:
            size = value.strip()
    return colour, size


def describe(row: dict[str, Any]) -> str:
    """The master name for a variant: product title, plus the variant title when
    it says something the product title does not."""
    product = (row.get("product_title") or "").strip()
    variant = (row.get("variant_title") or "").strip()
    if not variant or variant == DEFAULT_OPTION_VALUE or variant in product:
        return product or row["sku"]
    return f"{product} {variant}".strip()


async def plan(
    session: AsyncSession, catalogue: list[dict[str, Any]]
) -> tuple[list[Discovery], dict[str, int]]:
    """Work out what is missing. Reads only."""
    summary = {
        "shopify_variants": len(catalogue),
        "duplicate_skus": 0,
        "already_mapped": 0,
        "link_existing_master": 0,
        "create_master": 0,
    }
    if not catalogue:
        return [], summary

    seen = Counter(row["sku"] for row in catalogue)
    summary["duplicate_skus"] = sum(1 for n in seen.values() if n > 1)

    skus = list(seen)
    mapped = await session.execute(
        select(ChannelSkuMapping.channel_sku).where(
            ChannelSkuMapping.channel == CHANNEL,
            ChannelSkuMapping.channel_sku.in_(skus),
        )
    )
    # Deliberately NOT filtered on is_active. An inactive mapping is a decision
    # somebody made; re-creating it here would silently undo a deactivation.
    have_mapping = {s for (s,) in mapped.all()}

    by_code = await session.execute(
        select(MasterSku.sku_code, MasterSku.id).where(MasterSku.sku_code.in_(skus))
    )
    # dict(rows) does not type-check against SQLAlchemy Rows; same shape as
    # reconcile_inventory.py and audit_shopify_stock.py.
    master_by_code: dict[str, int] = {code: mid for code, mid in by_code.all()}  # noqa: C416

    found: list[Discovery] = []
    done: set[str] = set()
    for row in catalogue:
        sku = row["sku"]
        if sku in have_mapping:
            summary["already_mapped"] += 1
            continue
        if sku in done:
            continue  # a duplicated SKU registers once
        done.add(sku)
        colour, size = classify_options(row.get("options") or {})
        existing = master_by_code.get(sku)
        found.append(
            Discovery(
                sku=sku,
                name=describe(row),
                colour=colour,
                size=size,
                token=extract_token(row.get("product_title")),
                image_url=row.get("image_url") or "",
                existing_master_id=existing,
            )
        )
        key = "link_existing_master" if existing else "create_master"
        summary[key] += 1

    found = await _attach_related(session, found)
    summary["related_to_existing"] = sum(1 for d in found if d.related)
    return found, summary


async def _attach_related(session: AsyncSession, found: list[Discovery]) -> list[Discovery]:
    """Fill in `related` — the existing masters that share a product token.

    The token is matched as a PREFIX with a digit guard, so "N10" does not claim
    "N108gold". Matching on the token attribute instead would miss the masters
    imported before it was recorded, which is most of them.
    """
    tokens = {d.token for d in found if d.token}
    if not tokens:
        return found
    rows = await session.execute(
        select(MasterSku.sku_code).where(
            or_(*[MasterSku.sku_code.like(f"{t}%") for t in sorted(tokens)])
        )
    )
    by_token: dict[str, list[str]] = {}
    for (code,) in rows.all():
        for token in tokens:
            if re.match(rf"{re.escape(token)}(?![0-9])", code, re.IGNORECASE):
                by_token.setdefault(token, []).append(code)
    return [replace(d, related=tuple(sorted(by_token.get(d.token or "", [])))) for d in found]


async def apply(session: AsyncSession, found: list[Discovery]) -> None:
    """Create the missing masters and mappings. Adds only."""
    for item in found:
        master_id = item.existing_master_id
        if master_id is None:
            master = MasterSku(
                sku_code=item.sku,
                name=item.name,
                image_url=item.image_url or None,
                attributes={"token": item.token, "color": item.colour, "size": item.size},
            )
            session.add(master)
            await session.flush()
            master_id = master.id
        session.add(
            ChannelSkuMapping(
                master_sku_id=master_id,
                channel=CHANNEL,
                channel_sku=item.sku,
                is_active=True,
            )
        )


async def run(
    *,
    apply_changes: bool = False,
    session_factory: SessionFactory | None = None,
) -> int:
    factory = session_factory or async_session_factory

    adapter = build_shopify_adapter(purpose="sync product masters")
    try:
        catalogue = await adapter.fetch_catalogue()
    finally:
        close = getattr(adapter, "aclose", None) or getattr(adapter, "close", None)
        if close:
            await close()

    async with factory() as session:
        found, summary = await plan(session, catalogue)

        print("\n--- Shopify 商品マスタ同期 ---")
        for key, value in summary.items():
            print(f"  {key:22} {value}")

        if found:
            print("\n  未登録のバリアント:")
            for item in found:
                action = "マッピングのみ" if item.existing_master_id else "新規マスタ"
                axes = " / ".join(x for x in (item.colour, item.size) if x) or "-"
                print(f"    {item.sku:24} {axes:20} {action}  {item.name[:40]}")
                if item.related:
                    print(f"      └ 同一商品の既存マスタ: {', '.join(item.related)}")

        if not apply_changes:
            log.info("sync_masters.report", **summary)
            print("\n  ※ 登録していません (--apply で実行)")
            return 0

        if found:
            # NOT `async with session.begin()`. The planning reads above run on
            # this same session and autobegin a transaction, so opening a second
            # one raises InvalidRequestError. Committing the transaction the
            # reads started is also what makes plan-and-apply atomic.
            await apply(session, found)
            await session.commit()

    log.info("sync_masters.applied", **summary)
    print(f"\n  {len(found)}件を登録しました")
    print("  ※ 在庫は登録していません。棚卸または入荷で設定してください")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Register new Shopify products in the master")
    p.add_argument(
        "--apply",
        action="store_true",
        help="actually create the masters/mappings (default: report only)",
    )
    args = p.parse_args()
    configure_logging("INFO")
    sys.exit(asyncio.run(run(apply_changes=args.apply)))


if __name__ == "__main__":
    main()
