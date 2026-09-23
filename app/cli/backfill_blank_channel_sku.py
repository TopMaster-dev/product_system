"""Give the blank-SKU order lines a key they can actually be mapped by.

The Shopify adapter now derives `variant:<id>` when a line has no SKU, so new
orders arrive distinguishable. The rows already in the table do not: they hold
the empty string, and `inspect_blank_channel_sku` showed 46 lines and ¥385,660
behind one empty key in production, spread over 9 different products.

This rewrites those lines' `channel_sku` from the variant id kept in
`orders.raw_payload`, using the same derivation the adapter uses — so one
mapping covers both the history and everything that arrives from now on.

WHAT IT DOES NOT DO

No mapping, no stock, no `master_sku_id`. It only makes the lines addressable;
`/admin/mappings` (or a later `reresolve_order_items`) does the attribution.
Splitting it that way keeps this reversible in the only sense that matters: it
never guesses which product a line belongs to.

The stale alert on the empty key is closed as part of the run — leaving it open
would keep offering the operator the action that caused this.

Lines whose payload has no variant either are left alone and reported. They are
genuinely unidentifiable, and inventing a key for them would hide that.

    powershell -File scripts/run_cli.ps1 -Cli backfill_blank_channel_sku ^
        -Args "--dry-run"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.shopify import channel_sku_for
from app.cli.inspect_blank_channel_sku import line_details
from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import MappingAlert, MappingAlertStatusEnum, Order, OrderItem

log = get_logger(__name__)


@dataclass
class Outcome:
    rekeyed: int = 0
    unidentifiable: int = 0
    unidentifiable_sales_jpy: Decimal = Decimal(0)
    alerts_closed: int = 0
    #: new channel_sku -> product names seen under it, for the report.
    keys: dict[str, set[str]] = field(default_factory=lambda: defaultdict(set))


async def rekey(session: AsyncSession, *, channel: str | None = None) -> Outcome:
    blank = or_(OrderItem.channel_sku == "", func.trim(OrderItem.channel_sku) == "")
    stmt = (
        select(OrderItem, Order)
        .join(Order, Order.id == OrderItem.order_id)
        .where(blank, OrderItem.master_sku_id.is_(None))
        .order_by(Order.ordered_at)
    )
    if channel:
        stmt = stmt.where(Order.channel == channel)

    outcome = Outcome()
    for item, order in (await session.execute(stmt)).all():
        name, variant_id = line_details(order.raw_payload, item.line_id)
        # The adapter's own derivation, not a second copy of the rule: a
        # history keyed differently from what arrives tomorrow needs two
        # mappings for one product, which is the problem again in a new shape.
        new_key = channel_sku_for("", variant_id)
        if not new_key:
            outcome.unidentifiable += 1
            outcome.unidentifiable_sales_jpy += item.quantity * item.unit_price
            continue
        item.channel_sku = new_key
        outcome.rekeyed += 1
        outcome.keys[new_key].add(name or "(名称不明)")

    if outcome.rekeyed:
        await session.flush()
        outcome.alerts_closed = await _close_blank_alerts(session, channel=channel)
    return outcome


async def _close_blank_alerts(session: AsyncSession, *, channel: str | None) -> int:
    """Retire the alert on the empty key.

    It is not resolved — nothing was mapped — but it must stop being offered:
    it is the one action that would attribute every product behind it to a
    single master. New alerts appear under the real keys as orders arrive.
    """
    stmt = select(MappingAlert).where(
        or_(MappingAlert.channel_sku == "", func.trim(MappingAlert.channel_sku) == ""),
        MappingAlert.status.in_([MappingAlertStatusEnum.OPEN, MappingAlertStatusEnum.IN_PROGRESS]),
    )
    if channel:
        stmt = stmt.where(MappingAlert.channel == channel)

    closed = 0
    for alert in (await session.execute(stmt)).scalars().all():
        alert.status = MappingAlertStatusEnum.IGNORED.value
        closed += 1
    if closed:
        await session.flush()
    return closed


def report(outcome: Outcome, *, dry_run: bool) -> None:
    print(
        "\n  === 空欄の商品コードにキーを付与 ==="
        + ("  (dry-run: 保存しません)" if dry_run else "")
    )
    print(f"  キーを付与した明細   {outcome.rekeyed:>5}件")
    print(f"  新しいキー           {len(outcome.keys):>5}件")
    print(f"  取り下げたアラート   {outcome.alerts_closed:>5}件")

    if outcome.unidentifiable:
        print(
            f"\n  ★ バリアントIDも無く特定できない明細  {outcome.unidentifiable}件"
            f"  {outcome.unidentifiable_sales_jpy:,.0f} 円"
            "\n    商品が削除された可能性があります。手当ては別途必要です。"
        )

    if outcome.keys:
        print(f"\n    {'新しいキー':<28}商品名")
        for key, names in sorted(outcome.keys.items()):
            print(f"    {key:<28}{' / '.join(sorted(names))[:60]}")
        print("\n  この後 /admin/mappings で各キーを商品に紐づけてください。")


async def run(*, dry_run: bool = False, channel: str | None = None) -> int:
    async with async_session_factory() as session:
        outcome = await rekey(session, channel=channel)
        if dry_run:
            await session.rollback()
        else:
            await session.commit()

    report(outcome, dry_run=dry_run)
    log.info(
        "blank_sku_backfill.done",
        rekeyed=outcome.rekeyed,
        keys=len(outcome.keys),
        unidentifiable=outcome.unidentifiable,
        alerts_closed=outcome.alerts_closed,
        dry_run=dry_run,
    )
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Re-key blank channel SKUs from the variant id")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--channel", default=None, help="shopify / rakuten; default is every channel")
    args = p.parse_args()
    configure_logging("INFO")
    sys.exit(asyncio.run(run(dry_run=args.dry_run, channel=args.channel)))


if __name__ == "__main__":
    main()
