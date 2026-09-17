"""Make several SKUs draw on one pool of physical stock (共有在庫).

The client sells one physical item under several SKUs. N108 is the case that
prompted this: only the 53cm necklace is purchased, and the 42cm and 45cm
listings are the same chain cut shorter. Three Shopify variants, one pool.

The topology already exists and is in production — 22 anklet/bracelet groups use
it since the 2026-07-20 cutover. One master holds the stock (the POOL), and each
sellable SKU is a bundle master consuming `quantity_per=1` of it (a SHARE). Any
sale draws the one pool, and all of them display the same number.

What this adds is a way to declare a link that is not one of Phase 1-B's three
fixed CSVs. `import_variant_mappings` can only express the anklet/bracelet shape
it was written for.

Two properties worth knowing before running it:

* **Per-length sales history still accrues separately.** The analytics rollup
  aggregates sales without filtering bundles, so revenue lands on the SKU that
  actually sold while stock and velocity stay on the single pool. That is what
  makes the eventual split cheap.

* **Splitting later is the reverse of this, and needs no data migration.**
  Delete the bundle_components row and clear is_bundle, and
  `resolve_consumption` falls through to `[(self, 1)]` — an ordinary
  stock-holding master again. The per-length sales history is already separate
  by then, so nothing has to be rewritten.

Usage (via the Cloud SQL proxy). One pool per invocation:
    powershell -File scripts/run_cli.ps1 -Cli link_shared_stock ^
        -Args "--pool N108gold --share N108gold42 --share N108gold45"
    ... same again with -Apply to write.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import BundleComponent, InventorySnapshot, MasterSku

log = get_logger(__name__)
SessionFactory = async_sessionmaker[AsyncSession]


@dataclass(frozen=True, slots=True)
class Link:
    """A share that is ready to be linked to the pool."""

    sku: str
    master_id: int
    #: Already linked to this pool. Kept in the plan so a re-run reports it as
    #: done rather than silently omitting it, which reads as "not found".
    already_linked: bool


@dataclass(frozen=True, slots=True)
class Blocker:
    sku: str
    reason: str


@dataclass(frozen=True, slots=True)
class Plan:
    pool_id: int | None
    links: list[Link]
    blockers: list[Blocker]

    def ready(self) -> list[Link]:
        return [x for x in self.links if not x.already_linked]


async def plan(session: AsyncSession, pool_sku: str, share_skus: list[str]) -> Plan:
    """Decide what can be linked. Reads only, and reports every problem at once —
    fixing them one run at a time makes each repeat attempt look like a new
    failure."""
    wanted = [pool_sku, *share_skus]
    rows = await session.execute(
        select(MasterSku.id, MasterSku.sku_code, MasterSku.is_bundle).where(
            MasterSku.sku_code.in_(wanted)
        )
    )
    found = {code: (mid, is_bundle) for mid, code, is_bundle in rows.all()}

    blockers: list[Blocker] = []
    if pool_sku not in found:
        return Plan(None, [], [Blocker(pool_sku, "マスタが存在しません")])

    pool_id, pool_is_bundle = found[pool_sku]
    if pool_is_bundle:
        # docs/16: no nested sets. A pool that is itself derived has no stock of
        # its own, so every share would compute against nothing.
        return Plan(None, [], [Blocker(pool_sku, "在庫元が既に共有在庫の参照側です")])

    share_ids = [found[s][0] for s in share_skus if s in found]

    stock_rows = await session.execute(
        select(InventorySnapshot.master_sku_id, InventorySnapshot.on_hand_qty).where(
            InventorySnapshot.master_sku_id.in_(share_ids)
        )
    )
    on_hand = {mid: qty for mid, qty in stock_rows.all()}  # noqa: C416

    comp_rows = await session.execute(
        select(BundleComponent.bundle_master_sku_id, BundleComponent.component_master_sku_id).where(
            BundleComponent.bundle_master_sku_id.in_(share_ids)
        )
    )
    existing: dict[int, set[int]] = {}
    for bundle_id, component_id in comp_rows.all():
        existing.setdefault(bundle_id, set()).add(component_id)

    links: list[Link] = []
    for sku in share_skus:
        if sku not in found:
            blockers.append(Blocker(sku, "マスタが存在しません"))
            continue
        share_id, _ = found[sku]
        if share_id == pool_id:
            blockers.append(Blocker(sku, "在庫元と同じSKUです"))
            continue

        components = existing.get(share_id, set())
        if components == {pool_id}:
            links.append(Link(sku, share_id, already_linked=True))
            continue
        if components:
            blockers.append(Blocker(sku, "既に別のSKUの在庫を参照しています"))
            continue

        qty = on_hand.get(share_id, 0)
        if qty != 0:
            # Becoming a share means this master's own snapshot stops being
            # consulted - availability comes from the pool. Linking it would make
            # real stock silently disappear from every screen.
            reason = f"自身の在庫が {qty} 残っています。先に在庫元へ移してください"
            blockers.append(Blocker(sku, reason))
            continue

        links.append(Link(sku, share_id, already_linked=False))

    return Plan(pool_id, links, blockers)


async def apply(session: AsyncSession, result: Plan) -> None:
    """Link the shares to the pool. Adds only."""
    if result.pool_id is None:
        # Only reachable if a caller skips the blocker check; a NULL component
        # would be rejected by the FK anyway, but not before is_bundle was set.
        return
    for link in result.ready():
        share = await session.get(MasterSku, link.master_id)
        if share is not None:
            share.is_bundle = True
        session.add(
            BundleComponent(
                bundle_master_sku_id=link.master_id,
                component_master_sku_id=result.pool_id,
                quantity_per=1,
            )
        )


async def run(
    *,
    pool: str,
    shares: list[str],
    apply_changes: bool = False,
    session_factory: SessionFactory | None = None,
) -> int:
    factory = session_factory or async_session_factory
    async with factory() as session:
        result = await plan(session, pool, shares)

        print(f"\n--- 共有在庫リンク  在庫元: {pool} ---")
        for blocker in result.blockers:
            print(f"  {blocker.sku:24} 実行できません: {blocker.reason}")

        if result.blockers:
            log.error("link_shared_stock.blocked", pool=pool, blockers=len(result.blockers))
            print("\n  問題のあるSKUがあるため、何も登録していません")
            return 1

        ready = result.ready()
        if apply_changes and ready:
            # NOT `async with session.begin()`. The planning reads above run on
            # this same session and autobegin a transaction, so opening a second
            # one raises InvalidRequestError. Committing the transaction the
            # reads started is also what makes plan-and-apply atomic.
            await apply(session, result)
            await session.commit()

        # Printed AFTER the write, so the tense is the truth. Saying "will link"
        # on a run that linked, or "linked" on one that then failed, both leave
        # the operator unsure whether to run it again.
        for link in result.links:
            if link.already_linked:
                state = "リンク済み"
            else:
                state = "リンクしました" if apply_changes else "リンクします"
            print(f"  {link.sku:24} {state}")

        if not apply_changes:
            log.info("link_shared_stock.report", pool=pool, ready=len(ready))
            print(f"\n  {len(ready)}件をリンクできます  ※ 登録していません (--apply で実行)")
            return 0

    log.info("link_shared_stock.applied", pool=pool, linked=len(ready))
    print(f"\n  {len(ready)}件をリンクしました")
    print(f"  以降、これらのSKUの在庫は {pool} の在庫として表示・消費されます")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Make SKUs share one pool of stock")
    p.add_argument("--pool", required=True, help="the SKU that holds the physical stock")
    p.add_argument(
        "--share", action="append", default=[], required=True, help="a SKU drawing on the pool"
    )
    p.add_argument("--apply", action="store_true", help="write (default: report only)")
    args = p.parse_args()
    configure_logging("INFO")
    sys.exit(asyncio.run(run(pool=args.pool, shares=args.share, apply_changes=args.apply)))


if __name__ == "__main__":
    main()
