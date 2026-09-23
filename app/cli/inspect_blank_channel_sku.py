"""Read-only: what is hiding behind an empty channel SKU?

`order_items.channel_sku` is the key everything maps on. Shopify sends an empty
string when a variant has no SKU set, and the adapter stores it as-is — so the
empty string becomes a key like any other.

It is not like any other. A key is supposed to identify one product; an empty
one identifies every product that lacks a SKU. `mapping_alerts` is UNIQUE on
(channel, channel_sku, marketplace_id), so all of them collapse into a single
alert carrying whichever 商品名 arrived first — and resolving that alert in the
admin screen maps EVERY one of those lines onto that single master.

This prints what is actually behind the key, by reading the product names back
out of `orders.raw_payload`, so the question "how many different products?" has
a number instead of an assumption.

Reads nothing but our own database. Writes nothing.

    powershell -File scripts/run_cli.ps1 -Cli inspect_blank_channel_sku
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import MappingAlert, Order, OrderItem

log = get_logger(__name__)

_MAX_ROWS = 30


@dataclass
class Product:
    """One real product found behind the empty key."""

    name: str
    variant_ids: set[str] = field(default_factory=set)
    lines: int = 0
    units: int = 0
    sales_jpy: Decimal = Decimal(0)
    first_at: datetime | None = None
    last_at: datetime | None = None

    def add(self, *, variant_id: str, quantity: int, amount: Decimal, when: datetime) -> None:
        if variant_id:
            self.variant_ids.add(variant_id)
        self.lines += 1
        self.units += quantity
        self.sales_jpy += amount
        if self.first_at is None or when < self.first_at:
            self.first_at = when
        if self.last_at is None or when > self.last_at:
            self.last_at = when


def line_details(payload: Any, line_id: str) -> tuple[str, str]:
    """(product name, variant id) for one line, from either payload shape.

    The GraphQL poller stores the order node; the webhook stores the REST body.
    Both are in the table, so both are read here — a lookup that silently
    handles one shape would report "名称不明" for half the data and the count
    of distinct products would come out far too low.
    """
    if not isinstance(payload, dict):
        return ("", "")

    for edge in (payload.get("lineItems") or {}).get("edges", []) or []:
        node = edge.get("node") or {}
        if str(node.get("id", "")).rsplit("/", 1)[-1] == line_id:
            variant = (node.get("variant") or {}).get("id") or ""
            return (str(node.get("name") or ""), str(variant).rsplit("/", 1)[-1])

    for item in payload.get("line_items") or []:
        if str(item.get("id", "")) == line_id:
            return (str(item.get("name") or ""), str(item.get("variant_id") or ""))

    return ("", "")


async def collect(session: AsyncSession, *, channel: str | None = None) -> dict[str, Product]:
    blank = or_(OrderItem.channel_sku == "", func.trim(OrderItem.channel_sku) == "")
    stmt = (
        select(OrderItem, Order)
        .join(Order, Order.id == OrderItem.order_id)
        .where(blank, OrderItem.master_sku_id.is_(None))
        .order_by(Order.ordered_at)
    )
    if channel:
        stmt = stmt.where(Order.channel == channel)

    products: dict[str, Product] = {}
    for item, order in (await session.execute(stmt)).all():
        name, variant_id = line_details(order.raw_payload, item.line_id)
        key = name or f"(名称不明 variant={variant_id or '?'})"
        products.setdefault(key, Product(name=key)).add(
            variant_id=variant_id,
            quantity=item.quantity,
            amount=item.quantity * item.unit_price,
            when=order.ordered_at,
        )
    return products


async def blank_alerts(session: AsyncSession) -> list[tuple[str, str, int]]:
    """The alerts the empty key produced: (channel, 商品名, 発生件数)."""
    rows = await session.execute(
        select(
            MappingAlert.channel, MappingAlert.product_name, MappingAlert.occurrence_count
        ).where(or_(MappingAlert.channel_sku == "", func.trim(MappingAlert.channel_sku) == ""))
    )
    return [(c, name or "", int(n or 0)) for c, name, n in rows.all()]


def report(products: dict[str, Product], alerts: list[tuple[str, str, int]]) -> None:
    print("\n  商品コードが空欄の受注明細 — 読み取りのみ")

    if not products:
        print("\n  該当なし")
        return

    lines = sum(p.lines for p in products.values())
    total = sum((p.sales_jpy for p in products.values()), Decimal(0))
    variants = {v for p in products.values() for v in p.variant_ids}

    print(f"\n  明細 {lines}件 / 売上 {total:,.0f} 円")
    print(f"  この裏にいる商品は {len(products)}種類 / バリアント {len(variants)}件")

    if len(products) > 1:
        # The finding, stated rather than left to be inferred from the table.
        print(
            "\n  ★ 空欄は識別子になっていません。"
            f"\n    mapping_alerts は (チャネル, 商品コード) で一意なので、"
            f"{len(products)}種類が1件のアラートに潰れます。"
            "\n    このアラートを管理画面で解決すると、"
            f"{lines}明細すべてが同じ商品に紐づきます。"
        )

    print(f"\n    {'商品名':<44}{'明細':>6}{'点数':>6}{'売上':>12}  期間")
    ranked = sorted(products.values(), key=lambda p: p.lines, reverse=True)
    for p in ranked[:_MAX_ROWS]:
        span = ""
        if p.first_at and p.last_at:
            span = f"{p.first_at:%Y-%m-%d} 〜 {p.last_at:%Y-%m-%d}"
        print(f"    {p.name[:42]:<44}{p.lines:>6}{p.units:>6}{p.sales_jpy:>12,.0f}  {span}")
    if len(ranked) > _MAX_ROWS:
        print(f"    ... ほか {len(ranked) - _MAX_ROWS}種類")

    print("\n  --- 空欄キーが作ったアラート ---")
    if not alerts:
        print("    なし")
    else:
        for channel, name, occurrences in alerts:
            shown = name or "商品名なし"
            print(f"    {channel:<10}{shown[:44]:<46}発生 {occurrences}回")
        print("    ※ 商品名は最初に届いた1件のものです。他の商品はここに現れません。")


async def run(*, channel: str | None = None) -> int:
    async with async_session_factory() as session:
        products = await collect(session, channel=channel)
        alerts = await blank_alerts(session)

    report(products, alerts)
    log.info(
        "blank_channel_sku.done",
        products=len(products),
        lines=sum(p.lines for p in products.values()),
        alerts=len(alerts),
    )
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Show what an empty channel SKU is hiding")
    p.add_argument("--channel", default=None, help="shopify / rakuten; default is every channel")
    args = p.parse_args()
    configure_logging("INFO")
    sys.exit(asyncio.run(run(channel=args.channel)))


if __name__ == "__main__":
    main()
