"""Read-only: where did a SKU's stock number actually come from?

`inventory_snapshots` is a projection; `inventory_events` is the truth, and the
invariant is that the snapshot equals the sum of the events. So when a stock
figure looks wrong, the events say why — not as a guess about seeding or
double-counting, but as a breakdown per event type.

Written while diagnosing the Shopify audit: it reported our stock as roughly
five times Shopify's across the catalogue, with individual variants at 1300
against 0. Summary statistics could say the difference was systematic but not
what caused it, and every explanation on offer — seeding applied per variant
instead of per product, a repeated import, genuine warehouse stock never listed
online — predicts a DIFFERENT event breakdown. This prints the breakdown.

Reads nothing but our own database. Writes nothing.

Usage (via the Cloud SQL proxy):
    py -m app.cli.inspect_stock_origin --sku N16gold --sku N13gold
    py -m app.cli.inspect_stock_origin --top 10
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import InventoryEvent, InventorySnapshot, MasterSku

log = get_logger(__name__)
SessionFactory = async_sessionmaker[AsyncSession]


async def resolve_targets(
    session: AsyncSession, *, skus: list[str], top: int
) -> list[tuple[int, str]]:
    """The SKUs to explain: named ones, else the largest holdings."""
    stmt = select(MasterSku.id, MasterSku.sku_code)
    if skus:
        stmt = stmt.where(MasterSku.sku_code.in_(skus))
    else:
        stmt = (
            stmt.join(InventorySnapshot, InventorySnapshot.master_sku_id == MasterSku.id)
            .order_by(InventorySnapshot.on_hand_qty.desc())
            .limit(top)
        )
    return [(mid, code) for mid, code in (await session.execute(stmt)).all()]


@dataclass(frozen=True, slots=True)
class TypeTotal:
    event_type: str
    count: int
    total: int
    first: datetime | None
    last: datetime | None

    @property
    def span(self) -> str:
        first = self.first.date().isoformat() if self.first else "?"
        last = self.last.date().isoformat() if self.last else "?"
        return f"{first} 〜 {last}"


@dataclass(frozen=True, slots=True)
class Origin:
    snapshot: int
    events_sum: int
    by_type: list[TypeTotal]

    @property
    def invariant_holds(self) -> bool:
        """The Phase 1-B invariant: the projection equals the sum of the truth.

        A mismatch is a DIFFERENT fault from the stock merely being wrong — it
        means the snapshot drifted from its events, which no amount of counting
        shelves will explain.
        """
        return self.snapshot == self.events_sum


async def explain(session: AsyncSession, master_sku_id: int) -> Origin:
    """Snapshot, event totals per type, and whether the two agree."""
    snapshot = await session.scalar(
        select(InventorySnapshot.on_hand_qty).where(
            InventorySnapshot.master_sku_id == master_sku_id
        )
    )
    rows = (
        await session.execute(
            select(
                InventoryEvent.event_type,
                func.count().label("n"),
                func.sum(InventoryEvent.quantity_delta).label("total"),
                func.min(InventoryEvent.occurred_at).label("first"),
                func.max(InventoryEvent.occurred_at).label("last"),
            )
            .where(InventoryEvent.master_sku_id == master_sku_id)
            .group_by(InventoryEvent.event_type)
            .order_by(func.sum(InventoryEvent.quantity_delta).desc())
        )
    ).all()
    by_type = [
        TypeTotal(
            event_type=str(r.event_type),
            count=int(r.n),
            total=int(r.total or 0),
            first=r.first,
            last=r.last,
        )
        for r in rows
    ]
    return Origin(
        snapshot=int(snapshot or 0),
        events_sum=sum(t.total for t in by_type),
        by_type=by_type,
    )


async def run(
    *,
    skus: list[str] | None = None,
    top: int = 10,
    session_factory: SessionFactory | None = None,
) -> int:
    factory = session_factory or async_session_factory
    async with factory() as session:
        targets = await resolve_targets(session, skus=skus or [], top=top)
        if not targets:
            print("\n  該当するSKUがありません")
            return 0

        print("\n--- 在庫数の内訳 (読み取りのみ) ---")
        broken = 0
        for master_sku_id, code in targets:
            found = await explain(session, master_sku_id)
            log.info(
                "stock_origin",
                sku=code,
                snapshot=found.snapshot,
                events_sum=found.events_sum,
                invariant_holds=found.invariant_holds,
            )
            flag = "" if found.invariant_holds else "  ** スナップショットと不一致 **"
            print(f"\n  {code}  在庫 {found.snapshot}{flag}")
            if not found.invariant_holds:
                broken += 1
                print(f"    イベント合計 {found.events_sum}")
            for item in found.by_type:
                print(f"    {item.event_type:24} {item.total:>8}  ({item.count}件  {item.span})")
        if broken:
            print(f"\n  ※ {broken}件でスナップショットとイベント合計が一致しません")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Explain where a SKU's stock number came from")
    p.add_argument("--sku", action="append", default=[], help="Repeatable. Omit to use --top.")
    p.add_argument("--top", type=int, default=10, help="Largest holdings, when no --sku is given.")
    args = p.parse_args()
    configure_logging("INFO")
    sys.exit(asyncio.run(run(skus=args.sku, top=args.top)))


if __name__ == "__main__":
    main()
