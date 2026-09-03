"""Export the Rakuten unmapped-sales worksheet the client fills in.

The defect CSV mixes three 区分 and cannot be filled in and returned. What the
client actually needs is one sheet listing only the keys we cannot resolve, with
an empty column for the answer.

Scope is deliberately narrower than the defect report's 未マッピング実績 block.
That block lists every unresolved key, but some of those already HAVE an active
mapping and are unmapped only because the order predates it. Those need a remap
from us and nothing from the client, so asking about them wastes their time and
invites a confusing answer. `--all` includes them, for our own review only.

商品名 comes from mapping_alerts, which is where the Rakuten payload's itemName
is retained — order_items has no name column. It is a LEFT JOIN in effect: a key
with no alert still belongs on the sheet, just without a name.

最終受注日 earns its column. The question we are asking is "is this page still
live, or was it deleted?", and a key last sold eighteen months ago answers it
before the client has to look anything up.

Reads the database; writes one file.

Usage (via the Cloud SQL proxy):
    py -m app.cli.export_unmapped_worksheet
    py -m app.cli.export_unmapped_worksheet --all --out some/other.csv
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import Numeric, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import ChannelSkuMapping, MappingAlert, Order, OrderItem
from app.services.timeframe import to_jst_date
from app.ui.csv_export import UTF8_BOM, csv_body

log = get_logger(__name__)
SessionFactory = async_sessionmaker[AsyncSession]

CHANNEL = "rakuten"
DEFAULT_OUT = Path("csv_file/phase2/rakuten_unmapped_worksheet.csv")

HEADER = (
    "商品管理番号",
    "商品名",
    "受注明細数",
    "数量合計",
    "金額合計",
    "初回受注日",
    "最終受注日",
    "商品コード",
    "備考",
)


@dataclass(frozen=True, slots=True)
class WorksheetRow:
    manage_number: str
    product_name: str
    lines: int
    quantity: int
    amount: int
    first_date: str
    last_date: str

    def as_csv(self) -> tuple[str | int, ...]:
        # The last two are the client's to fill: 商品コード, and 備考 where
        # "対象外" marks a page they have since deleted.
        return (
            self.manage_number,
            self.product_name,
            self.lines,
            self.quantity,
            self.amount,
            self.first_date,
            self.last_date,
            "",
            "",
        )


def _jst(moment: datetime | None) -> str:
    return to_jst_date(moment).isoformat() if moment else ""


async def collect_rows(session: AsyncSession, *, include_mapped_keys: bool) -> list[WorksheetRow]:
    """One row per unresolved Rakuten channel_sku, most recent activity first."""
    stmt = (
        select(
            OrderItem.channel_sku,
            func.count().label("lines"),
            func.sum(OrderItem.quantity).label("quantity"),
            func.sum(OrderItem.quantity * OrderItem.unit_price)
            .cast(Numeric(14, 2))
            .label("amount"),
            func.min(Order.ordered_at).label("first_at"),
            func.max(Order.ordered_at).label("last_at"),
        )
        .join(Order, Order.id == OrderItem.order_id)
        .where(Order.channel == CHANNEL, OrderItem.master_sku_id.is_(None))
        .group_by(OrderItem.channel_sku)
        .order_by(func.max(Order.ordered_at).desc())
    )
    if not include_mapped_keys:
        stmt = stmt.where(
            OrderItem.channel_sku.not_in(
                select(ChannelSkuMapping.channel_sku).where(
                    ChannelSkuMapping.channel == CHANNEL,
                    ChannelSkuMapping.is_active.is_(True),
                )
            )
        )
    aggregated = (await session.execute(stmt)).all()
    if not aggregated:
        return []

    # Names in one query rather than one per key.
    name_rows = (
        await session.execute(
            select(MappingAlert.channel_sku, func.max(MappingAlert.product_name))
            .where(
                MappingAlert.channel == CHANNEL,
                MappingAlert.channel_sku.in_([row.channel_sku for row in aggregated]),
            )
            .group_by(MappingAlert.channel_sku)
        )
    ).all()
    names: dict[str, str] = {key: value for key, value in name_rows if value}

    return [
        WorksheetRow(
            manage_number=row.channel_sku,
            product_name=names.get(row.channel_sku, ""),
            lines=int(row.lines or 0),
            quantity=int(row.quantity or 0),
            # No decimal point: these are yen, and Excel renders 12000.00 as a
            # price the client then has to mentally strip.
            amount=int(row.amount or 0),
            first_date=_jst(row.first_at),
            last_date=_jst(row.last_at),
        )
        for row in aggregated
    ]


async def fetch(
    *,
    include_mapped_keys: bool = False,
    session_factory: SessionFactory | None = None,
) -> list[WorksheetRow]:
    """The database half, kept separate so the coroutine does no file IO."""
    factory = session_factory or async_session_factory
    async with factory() as session:
        return await collect_rows(session, include_mapped_keys=include_mapped_keys)


def write_worksheet(rows: list[WorksheetRow], out: Path) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    # UTF-8 + BOM: Excel decides a CSV's encoding by the BOM alone and mangles
    # Japanese without it, whatever we intended. See app/ui/csv_export.py.
    body = csv_body(HEADER, [row.as_csv() for row in rows])
    out.write_text(UTF8_BOM + body, encoding="utf-8", newline="")


def run(
    *,
    out: Path,
    include_mapped_keys: bool = False,
    session_factory: SessionFactory | None = None,
) -> int:
    rows = asyncio.run(
        fetch(include_mapped_keys=include_mapped_keys, session_factory=session_factory)
    )
    write_worksheet(rows, out)

    lines = sum(row.lines for row in rows)
    quantity = sum(row.quantity for row in rows)
    log.info(
        "unmapped_worksheet.written",
        path=str(out),
        keys=len(rows),
        lines=lines,
        quantity=quantity,
        include_mapped_keys=include_mapped_keys,
    )
    print(f"\n  {out}  ({len(rows)} 件)")
    if rows:
        print(f"  受注明細 {lines} 行 / 数量合計 {quantity}")
        print(f"  最終受注日 {rows[-1].last_date} 〜 {rows[0].last_date}")
        named = sum(1 for row in rows if row.product_name)
        print(f"  商品名あり {named} / {len(rows)}")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Export the Rakuten unmapped-sales worksheet")
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument(
        "--all",
        dest="include_mapped_keys",
        action="store_true",
        help="Include keys that already have a mapping (we remap those; do not send).",
    )
    args = p.parse_args()
    configure_logging("INFO")
    sys.exit(run(out=args.out, include_mapped_keys=args.include_mapped_keys))


if __name__ == "__main__":
    main()
