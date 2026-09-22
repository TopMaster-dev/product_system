"""Print a count sheet for a physical stocktake (P2-028).

The client counts in instalments, by category — confirmed 2026-09-22. RING is
265 SKUs, KEYRING is 3, and one day covers one group. So the sheet is produced
per category rather than as one 678-row list nobody can work through.

WHAT IS PRINTED AND WHY

`counted_qty` is left BLANK. Pre-filling it with the system's figure would turn
a count into a confirmation: the eye reads the printed number, the shelf looks
about right, and the number is copied across. The recorded figure is printed in
its own column so a counter can see when they disagree, but the cell they write
in starts empty.

The header matches what the importer reads, so an untouched round trip needs no
editing. The columns after `counted_qty` are for people, and the importer
ignores them.

    powershell -File scripts/run_cli.ps1 -Cli export_stocktake_sheet ^
        -Args "--categories RING,PIERCE"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy import select

from app.csv_export import UTF8_BOM, csv_body
from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import ProductCategory
from app.services.stocktake import COL_COUNTED, COL_SKU, sheet_rows

log = get_logger(__name__)

DEFAULT_OUT = Path("csv_file/phase2/stocktake")


async def run(*, categories: list[str], out_dir: Path = DEFAULT_OUT, split: bool = True) -> int:
    async with async_session_factory() as session:
        known = await session.execute(select(ProductCategory.code, ProductCategory.name))
        # dict(rows) does not type-check against SQLAlchemy Rows; same
        # shape as reconcile_inventory.py and audit_shopify_stock.py.
        names = {code: name for code, name in known.all()}  # noqa: C416

        unknown = [c for c in categories if c not in names]
        if unknown:
            print(f"未知のカテゴリコードです: {', '.join(unknown)}")
            print(f"指定可能: {', '.join(sorted(names))}")
            return 2

        groups: list[tuple[str, list[str]]]
        if not categories:
            groups = [("all", [])]
        elif split:
            # One file per category, because one file per counting day is what
            # the client asked for. Handing them a combined sheet and expecting
            # them to filter it is how half a category gets skipped.
            groups = [(code, [code]) for code in categories]
        else:
            groups = [("-".join(categories), categories)]

        written = 0
        for label, codes in groups:
            rows = await sheet_rows(session, category_codes=codes or None)
            if not rows:
                print(f"  {label:<12} 対象SKUがありません")
                continue
            body = csv_body(
                [COL_SKU, "商品名", "カテゴリ", "システム在庫数", COL_COUNTED, "備考"],
                # counted_qty deliberately blank — see the module docstring.
                [[r.sku_code, r.name, r.category or "未分類", r.on_hand_qty, "", ""] for r in rows],
            )
            path = out_dir / f"stocktake_{label}.csv"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(UTF8_BOM + body, encoding="utf-8")
            print(f"  {label:<12} {len(rows):>4}件  -> {path}")
            written += len(rows)

    print(f"\n  合計 {written}件")
    print("  ※ 実数の記入欄は空欄です。印字済みの在庫数は照合用で、転記しないでください")
    log.info("stocktake_sheet.done", rows=written, groups=len(groups))
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Export a physical count sheet")
    p.add_argument(
        "--categories",
        default="",
        help="comma-separated category codes; empty exports every SKU in one file",
    )
    p.add_argument(
        "--combined",
        action="store_true",
        help="one file for all the given categories instead of one file each",
    )
    p.add_argument("--out-dir", default=str(DEFAULT_OUT))
    args = p.parse_args()
    configure_logging("INFO")
    codes = [c.strip().upper() for c in args.categories.split(",") if c.strip()]
    sys.exit(
        asyncio.run(run(categories=codes, out_dir=Path(args.out_dir), split=not args.combined))
    )


if __name__ == "__main__":
    main()
