"""Analyze CROSS MALL inventory_informaiton.csv vs current production state.

Goals:
1. Parse the variant-level stock (商品コード × 属性1 × 属性2 → 在庫数量)
2. Aggregate to master-product level (商品コード)
3. Compare to the values we previously seeded from item_*.csv 総在庫数量
4. Compare to current production InventorySnapshot.on_hand_qty
5. Identify: (a) deltas to apply, (b) orphan codes in either direction,
   (c) negatives that need client confirmation
"""

from __future__ import annotations

import asyncio
import csv
import os
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).parent
INV_CSV = ROOT / "inventory_informaiton.csv"
PROD_CSV = ROOT / "sku" / "item_0601004857_000419.csv"
ENC = "cp932"


def read_inventory_csv() -> dict[str, list[tuple[str, str, int]]]:
    """Return {商品コード: [(attr1, attr2, qty), ...]}."""
    rows: dict[str, list[tuple[str, str, int]]] = defaultdict(list)
    with INV_CSV.open(encoding=ENC, newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        code_i = header.index("商品コード")
        a1_i = header.index("属性１名")
        a2_i = header.index("属性２名")
        qty_i = header.index("在庫数量")
        for r in reader:
            if len(r) <= qty_i:
                continue
            code = r[code_i]
            if not code:
                continue
            try:
                qty = int(r[qty_i] or "0")
            except ValueError:
                continue
            rows[code].append((r[a1_i], r[a2_i], qty))
    return dict(rows)


def read_product_csv() -> dict[str, int]:
    """Return {商品コード: 総在庫数量} from the older product CSV."""
    out: dict[str, int] = {}
    with PROD_CSV.open(encoding=ENC, newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        code_i = header.index("商品コード")
        qty_i = header.index("総在庫数量")
        for r in reader:
            if len(r) <= qty_i:
                continue
            code = r[code_i]
            if not code:
                continue
            try:
                qty = int(r[qty_i] or "0")
            except ValueError:
                continue
            out.setdefault(code, qty)
    return out


async def fetch_prod_snapshots() -> dict[str, int]:
    sys.path.insert(0, str(ROOT.parent))
    from app.db import async_session_factory
    from sqlalchemy import text
    out: dict[str, int] = {}
    async with async_session_factory() as s:
        r = await s.execute(text("""
            SELECT m.sku_code, COALESCE(snap.on_hand_qty, 0)
            FROM master_skus m
            LEFT JOIN inventory_snapshots snap ON snap.master_sku_id = m.id
        """))
        for code, qty in r.all():
            out[code] = qty
    return out


def main() -> None:
    inv = read_inventory_csv()
    old = read_product_csv()
    prod = asyncio.run(fetch_prod_snapshots())

    # Aggregate inventory CSV to product level.
    inv_agg = {code: sum(q for _, _, q in rows) for code, rows in inv.items()}
    inv_rowcount = sum(len(v) for v in inv.values())

    print("="*70)
    print(f"inventory_informaiton.csv: {inv_rowcount} variant rows across "
          f"{len(inv_agg)} unique 商品コード")
    print(f"old item_*.csv 総在庫数量:  {len(old)} unique 商品コード")
    print(f"prod master_skus:           {len(prod)} rows")
    print()

    # Codes present only in inventory CSV (not in old, not in master_skus)
    only_in_inv = sorted(set(inv_agg) - set(prod))
    only_in_prod = sorted(set(prod) - set(inv_agg))
    print(f"codes in inventory CSV but NOT in master_skus: {len(only_in_inv)}")
    if only_in_inv:
        print("  first 15:", only_in_inv[:15])
    print(f"codes in master_skus but NOT in inventory CSV: {len(only_in_prod)}")
    if only_in_prod:
        print("  first 15:", only_in_prod[:15])
    print()

    # Distribution
    pos = sum(1 for v in inv_agg.values() if v > 0)
    zero = sum(1 for v in inv_agg.values() if v == 0)
    neg = sum(1 for v in inv_agg.values() if v < 0)
    print(f"aggregated stock distribution: positive={pos}  zero={zero}  negative={neg}")
    print(f"  sum of all aggregated stock = {sum(inv_agg.values()):,}")
    print()

    # Negative aggregated codes
    neg_codes = sorted(((c, q) for c, q in inv_agg.items() if q < 0), key=lambda x: x[1])
    print(f"=== aggregate-negative codes ({len(neg_codes)}) ===")
    for c, q in neg_codes[:15]:
        print(f"  {c:25s} agg_qty={q:6d}")
    if len(neg_codes) > 15:
        print(f"  ...and {len(neg_codes)-15} more")
    print()

    # Negative variant-level rows (even within products that aggregate positive)
    neg_variants = []
    for code, rows in inv.items():
        for a1, a2, q in rows:
            if q < 0:
                neg_variants.append((code, a1, a2, q))
    print(f"=== variant-level negatives: {len(neg_variants)} variant rows ===")
    for code, a1, a2, q in sorted(neg_variants, key=lambda x: x[3])[:10]:
        print(f"  {code:25s} {a1:8s} {a2:30s} qty={q:6d}")
    print()

    # Compare old vs new totals
    print("=== old 総在庫数量 vs new aggregated 在庫数量 ===")
    changed = []
    for code in sorted(set(inv_agg) | set(old)):
        new_v = inv_agg.get(code)
        old_v = old.get(code)
        if new_v != old_v:
            changed.append((code, old_v, new_v))
    print(f"  codes with differing totals: {len(changed)} / {len(set(inv_agg) | set(old))}")
    for code, old_v, new_v in changed[:15]:
        delta = (new_v or 0) - (old_v or 0)
        print(f"  {code:25s} old={old_v}  new={new_v}  delta={delta:+}")
    if len(changed) > 15:
        print(f"  ...and {len(changed)-15} more")
    print()

    # Compare prod snapshot vs new aggregated CSV (this is the delta we'd apply
    # if we re-seed). prod = old_seed + recent_orders. new = CROSS MALL's view.
    print("=== current prod snapshot vs new aggregated CSV ===")
    snap_changes = []
    for code in sorted(set(inv_agg) | set(prod)):
        new_v = inv_agg.get(code)
        prod_v = prod.get(code, 0)
        if new_v is not None and new_v != prod_v:
            snap_changes.append((code, prod_v, new_v, new_v - prod_v))
    snap_changes.sort(key=lambda x: -abs(x[3]))
    print(f"  codes where prod ≠ new aggregate: {len(snap_changes)}")
    for code, prod_v, new_v, delta in snap_changes[:20]:
        print(f"  {code:25s} prod={prod_v:6d}  new={new_v:6d}  delta={delta:+}")


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    main()
