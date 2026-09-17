"""Daily stock audit against Shopify — the replacement for the CROSS MALL CSV.

P2-035. When CROSS MALL shuts down (client: early October 2026) the daily
reconciliation loses its input, and with it the only external check that our
event-sourced stock still matches reality. Shopify holds its own inventory
numbers for the same goods, so it can answer the same question.

Deliberately NOT a new mechanism. It builds the same `DiffInput` list the CROSS
MALL CLI builds and hands it to the same `ReconcileService.start_run`, tagged
`run_type='shopify_audit'` (migration 0012). The approval path that writes a
stocktake event and overwrites a snapshot is the riskiest code in the system;
there is one of it, and this is a third caller rather than a second copy.

THE RULE THAT MATTERS: absent is not zero.

Only SKUs Shopify actually reported are compared. A master with no Shopify
variant, or one Shopify does not stock at this location, is counted and skipped
— never turned into a diff proposing its stock be zeroed. Getting that wrong
would empty the stock of every SKU sold only on Rakuten, and the approval queue
would present it as a routine correction.

Usage (via the Cloud SQL proxy, Shopify credentials required):
    powershell -File scripts/run_cli.ps1 -Cli audit_shopify_stock -WithShopify -DryRun
    powershell -File scripts/run_cli.ps1 -Cli audit_shopify_stock -WithShopify
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.cli._adapters import build_shopify_adapter
from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import (
    ChannelSkuMapping,
    InventorySnapshot,
    MasterSku,
    ReconcileRunTypeEnum,
)
from app.services.reconcile import DiffInput, ReconcileService
from app.services.sku_scope import analysable_conditions
from app.services.timeframe import to_jst_date

log = get_logger(__name__)
SessionFactory = async_sessionmaker[AsyncSession]

CHANNEL = "shopify"
SOURCE = "shopify_api"


#: Shopify tracks both. `on_hand` is physical stock; `available` subtracts what
#: is committed to unfulfilled orders. Which one our snapshots correspond to is
#: an empirical question — see the dry run, which reports against both.
QUANTITY_FIELDS = ("on_hand", "available")


def aggregate_levels(
    levels: list[dict[str, Any]], *, field: str = "on_hand"
) -> tuple[dict[str, int], int]:
    """Variant rows -> {sku: total quantity}, plus how many SKUs were duplicated.

    Shopify allows one SKU on several variants, each with its own inventory
    item. Sum them — the total for that SKU is what our single master means —
    but count the duplicates too: it is usually a catalogue mistake, and
    unreported it would surface only as an unexplained stock difference.
    """
    qty_by_sku: dict[str, int] = defaultdict(int)
    seen: dict[str, int] = defaultdict(int)
    for row in levels:
        sku = str(row["sku"])
        qty_by_sku[sku] += int(row[field])
        seen[sku] += 1
    return dict(qty_by_sku), sum(1 for n in seen.values() if n > 1)


def describe(diffs: list[DiffInput]) -> dict[str, int]:
    """The SHAPE of a difference set, which is what says whether it is credible.

    A handful of small differences is normal drift. Hundreds, all in the same
    direction, is a systematic cause — the wrong quantity compared, or two
    numbers that were never synchronised in the first place — and reads as
    "our stock is wrong" only if nobody looks at the distribution.
    """
    deltas = sorted(d.target_qty - d.current_qty for d in diffs)
    magnitudes = sorted(abs(x) for x in deltas)
    return {
        "diffs": len(deltas),
        "shopify_higher": sum(1 for x in deltas if x > 0),
        "shopify_lower": sum(1 for x in deltas if x < 0),
        "median_abs": magnitudes[len(magnitudes) // 2] if magnitudes else 0,
        "max_abs": magnitudes[-1] if magnitudes else 0,
    }


async def sample_rows(
    session: AsyncSession, diffs: list[DiffInput], *, limit: int = 8
) -> list[tuple[str, int, int]]:
    """(sku_code, ours, Shopify) for the biggest differences and some typical ones.

    Summary statistics say a difference set is systematic; only actual rows say
    what the system is. Two numbers side by side — 120 here, 2 there — settle in
    one glance what a median cannot.
    """
    if not diffs:
        return []
    ranked = sorted(diffs, key=lambda d: abs(d.target_qty - d.current_qty), reverse=True)
    middle = len(ranked) // 2
    picked = ranked[: limit // 2] + ranked[middle : middle + (limit - limit // 2)]
    codes = await session.execute(
        select(MasterSku.id, MasterSku.sku_code).where(
            MasterSku.id.in_([d.master_sku_id for d in picked])
        )
    )
    by_id = {mid: code for mid, code in codes.all()}  # noqa: C416
    return [
        (by_id.get(d.master_sku_id, str(d.master_sku_id)), d.current_qty, d.target_qty)
        for d in picked
    ]


async def collect_diffs(
    session: AsyncSession, levels: list[dict[str, Any]], *, field: str = "on_hand"
) -> tuple[list[DiffInput], dict[str, int]]:
    """Compare Shopify quantities against ours, for the SKUs Shopify reported."""
    # A variant Shopify is not tracking reports 0, and that 0 means "not counted
    # here" rather than "none in stock". Auditing against it would propose
    # zeroing real stock — the same failure as treating an absent SKU as zero,
    # wearing a number instead of a gap.
    tracked = [row for row in levels if row.get("tracked", True)]
    qty_by_sku, duplicates = aggregate_levels(tracked, field=field)
    summary: dict[str, int] = {
        "shopify_variants": len(levels),
        "untracked_variants": len(levels) - len(tracked),
        "duplicate_skus": duplicates,
        "unmapped_skus": 0,
        "excluded_bundles": 0,
        "excluded_unmanaged": 0,
        "excluded_archived": 0,
        "matched_masters": 0,
        "actual_diffs": 0,
    }
    if not qty_by_sku:
        return [], summary

    mapped = await session.execute(
        select(ChannelSkuMapping.channel_sku, ChannelSkuMapping.master_sku_id).where(
            ChannelSkuMapping.channel == CHANNEL,
            ChannelSkuMapping.channel_sku.in_(list(qty_by_sku)),
            ChannelSkuMapping.is_active.is_(True),
        )
    )
    sku_to_master: dict[str, int] = {cs: mid for cs, mid in mapped.all()}  # noqa: C416
    summary["unmapped_skus"] = len(qty_by_sku) - len(sku_to_master)

    # Several Shopify SKUs can map to one master (shared stock); sum them, the
    # same way the CROSS MALL path sums alias 商品コード.
    target_by_master: dict[int, int] = defaultdict(int)
    for sku, qty in qty_by_sku.items():
        master_id = sku_to_master.get(sku)
        if master_id is not None:
            target_by_master[master_id] += qty

    if target_by_master:
        in_scope = await session.execute(
            select(MasterSku.id).where(
                MasterSku.id.in_(list(target_by_master)),
                *analysable_conditions(include_archived=False),
            )
        )
        keep = {mid for (mid,) in in_scope.all()}
        excluded = [mid for mid in target_by_master if mid not in keep]
        if excluded:
            reasons = await session.execute(
                select(
                    MasterSku.id,
                    MasterSku.is_bundle,
                    MasterSku.is_stock_managed,
                    MasterSku.archived_at,
                ).where(MasterSku.id.in_(excluded))
            )
            for mid, is_bundle, is_managed, archived_at in reasons.all():
                target_by_master.pop(mid, None)
                if is_bundle:
                    summary["excluded_bundles"] += 1
                elif not is_managed:
                    summary["excluded_unmanaged"] += 1
                elif archived_at is not None:
                    summary["excluded_archived"] += 1
    summary["matched_masters"] = len(target_by_master)

    diffs: list[DiffInput] = []
    if target_by_master:
        snaps = await session.execute(
            select(InventorySnapshot.master_sku_id, InventorySnapshot.on_hand_qty).where(
                InventorySnapshot.master_sku_id.in_(list(target_by_master))
            )
        )
        current: dict[int, int] = {mid: q for mid, q in snaps.all()}  # noqa: C416
        for master_id, target_qty in target_by_master.items():
            current_qty = current.get(master_id, 0)
            if current_qty != target_qty:
                diffs.append(
                    DiffInput(
                        master_sku_id=master_id,
                        current_qty=current_qty,
                        target_qty=target_qty,
                    )
                )
    summary["actual_diffs"] = len(diffs)
    return diffs, summary


async def run(
    *,
    triggered_by: str = "cloud_scheduler",
    dry_run: bool = False,
    now: datetime | None = None,
    session_factory: SessionFactory | None = None,
) -> int:
    factory = session_factory or async_session_factory
    moment = now or datetime.now(UTC)

    adapter = build_shopify_adapter(purpose="audit stock")
    try:
        levels = await adapter.fetch_stock_levels()
    finally:
        close = getattr(adapter, "aclose", None) or getattr(adapter, "close", None)
        if close:
            await close()

    async with factory() as session:
        diffs, summary = await collect_diffs(session, levels)

        if dry_run:
            log.info("shopify_audit.dry_run", **summary)
            print("\n--- Shopify 在庫突合 (dry-run) ---")
            for key, value in summary.items():
                print(f"  {key:20} {value}")

            # A high diff count does not by itself say our numbers are wrong —
            # it can equally mean we are comparing the wrong quantity, or that
            # the two figures were never synchronised. Report enough shape to
            # tell those apart before anyone acts on the result.
            print("\n  --- 診断 ---")
            for candidate in QUANTITY_FIELDS:
                other, _ = await collect_diffs(session, levels, field=candidate)
                shape = describe(other)
                print(
                    f"  {candidate:10} 差分 {shape['diffs']:4}  "
                    f"Shopifyが多い {shape['shopify_higher']:4} / "
                    f"少ない {shape['shopify_lower']:4}  "
                    f"中央値 |差| {shape['median_abs']}  最大 |差| {shape['max_abs']}"
                )

            # Totals separate "a few SKUs are wrong" from "these are two
            # different kinds of number" — e.g. warehouse stock here against a
            # quantity allocated for online sale there.
            ours = sum(d.current_qty for d in diffs)
            theirs = sum(d.target_qty for d in diffs)
            print(f"\n  差分のある{len(diffs)}件の合計   当方 {ours}  /  Shopify {theirs}")

            print("\n  例 (SKU / 当方 / Shopify):")
            for code, current, target in await sample_rows(session, diffs):
                print(f"    {code:24} {current:6} {target:6}")
            print("\n  ※ 差分は登録していません")
            return 0

        # NOT `async with session.begin()`. `collect_diffs` above reads on this
        # same session, which autobegins a transaction; opening a second one
        # raises InvalidRequestError. Only the --dry-run path had ever been
        # exercised, so this would have failed on the audit's first real run.
        svc = ReconcileService(session)
        created = await svc.start_run(
            source=SOURCE,
            triggered_by=triggered_by,
            diffs=iter(diffs),
            run_type=ReconcileRunTypeEnum.SHOPIFY_AUDIT,
            counted_by=SOURCE,
            counted_on=to_jst_date(moment),
            scope_note=(
                "Shopify在庫との自動突合。Shopifyが返したSKUのみを対象とし、"
                "未報告のSKUはゼロ化しない"
            ),
            counted_sku_count=summary["matched_masters"],
        )
        run_id = created.id
        await session.commit()

    log.info("shopify_audit.done", run_id=run_id, **summary)
    print(f"\n--- Shopify 在庫突合 完了 (run #{run_id}) ---")
    for key, value in summary.items():
        print(f"  {key:20} {value}")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Audit our stock against Shopify inventory")
    p.add_argument("--triggered-by", default="cloud_scheduler")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    configure_logging("INFO")
    sys.exit(asyncio.run(run(triggered_by=args.triggered_by, dry_run=args.dry_run)))


if __name__ == "__main__":
    main()
