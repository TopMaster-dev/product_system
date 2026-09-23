"""Read-only: which master SKU is this product?

The mapping screen asks for a `master_sku_code`, and the thing you have in hand
is a product name off an order line, such as the SILVER 22cm variant of the
316L infinity anklet #B74. Nothing on the screen searches master SKUs by name,
so the answer was being eyeballed from a 1,000-row list.

Guessing there is expensive in a specific way: a mapping is silent when wrong.
Attach `#B74 SILVER` to the GOLD master and the sale simply lands on the other
colour for ever, with no error anywhere.

So this prints the candidates with enough context to choose between them —
current stock, whether the master is archived or 在庫管理対象外, and which
channel SKUs already map to it. A master that already carries a mapping for the
same channel is usually the wrong answer: that product is spoken for.

Reads nothing but our own database. Writes nothing.

    powershell -File scripts/run_cli.ps1 -Cli inspect_master_skus -Args "--q B74"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import ChannelSkuMapping, InventorySnapshot, MasterSku

log = get_logger(__name__)

_MAX_ROWS = 40


@dataclass(frozen=True, slots=True)
class Candidate:
    sku_code: str
    name: str
    on_hand: int
    archived: bool
    unmanaged: bool
    is_bundle: bool
    mappings: str

    @property
    def flags(self) -> str:
        marks = []
        if self.archived:
            marks.append("アーカイブ")
        if self.unmanaged:
            marks.append("在庫管理対象外")
        if self.is_bundle:
            marks.append("共有在庫の親")
        return " ".join(marks)


async def search(session: AsyncSession, *, query: str, channel: str | None) -> list[Candidate]:
    like = f"%{query}%"
    rows = await session.execute(
        select(
            MasterSku.id,
            MasterSku.sku_code,
            MasterSku.name,
            MasterSku.archived_at,
            MasterSku.is_stock_managed,
            MasterSku.is_bundle,
            func.coalesce(InventorySnapshot.on_hand_qty, 0),
        )
        .outerjoin(InventorySnapshot, InventorySnapshot.master_sku_id == MasterSku.id)
        .where(or_(MasterSku.sku_code.ilike(like), MasterSku.name.ilike(like)))
        .order_by(MasterSku.sku_code)
    )
    found = list(rows.all())
    if not found:
        return []

    # Which channel SKUs already point at these masters. A master that is
    # already spoken for on this channel is usually not the answer.
    ids = [r[0] for r in found]
    mapping_stmt = select(
        ChannelSkuMapping.master_sku_id,
        ChannelSkuMapping.channel,
        ChannelSkuMapping.channel_sku,
    ).where(ChannelSkuMapping.master_sku_id.in_(ids), ChannelSkuMapping.is_active.is_(True))
    if channel:
        mapping_stmt = mapping_stmt.where(ChannelSkuMapping.channel == channel)

    by_master: dict[int, list[str]] = {}
    for master_id, ch, ch_sku in (await session.execute(mapping_stmt)).all():
        by_master.setdefault(master_id, []).append(f"{ch}:{ch_sku}")

    return [
        Candidate(
            sku_code=code,
            name=name,
            on_hand=int(on_hand or 0),
            archived=archived is not None,
            unmanaged=not managed,
            is_bundle=bool(bundle),
            mappings=", ".join(sorted(by_master.get(mid, []))[:3]),
        )
        for mid, code, name, archived, managed, bundle, on_hand in found
    ]


def report(query: str, candidates: list[Candidate]) -> None:
    print(f"\n  === マスターSKU検索: {query} ===")
    if not candidates:
        print("  該当なし")
        return

    print(f"\n    {'SKUコード':<20}{'在庫':>6}  {'区分':<18}{'商品名':<40}既存マッピング")
    for c in candidates[:_MAX_ROWS]:
        print(f"    {c.sku_code:<20}{c.on_hand:>6}  {c.flags:<18}{c.name[:38]:<40}{c.mappings}")
    if len(candidates) > _MAX_ROWS:
        print(f"    ... ほか {len(candidates) - _MAX_ROWS}件")
    print(f"\n  {len(candidates)}件")


async def run(*, query: str, channel: str | None = None) -> int:
    async with async_session_factory() as session:
        candidates = await search(session, query=query, channel=channel)
    report(query, candidates)
    log.info("master_sku_search.done", query=query, found=len(candidates))
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Find a master SKU by code or product name")
    p.add_argument("--q", required=True, help="substring of the SKU code or the product name")
    p.add_argument("--channel", default=None, help="only show existing mappings for this channel")
    args = p.parse_args()
    configure_logging("INFO")
    sys.exit(asyncio.run(run(query=args.q, channel=args.channel)))


if __name__ == "__main__":
    main()
