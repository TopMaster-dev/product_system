"""Analyze the four CROSS MALL CSV files: detect schema, sample data,
and check whether they together let us auto-resolve our 165 unmapped alerts."""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parent
ENC = "cp932"

FILES = {
    "master_products": ROOT / "sku" / "item_0601004857_000419.csv",
    "master_skus": ROOT / "sku" / "item_sku_0601014313_000409.csv",
    "rakuten": ROOT / "rakuten" / "normal-item_0601005523_405.csv",
    "shopify": ROOT / "shopify" / "item_0601005457_197.csv",
}


def load_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    with path.open("r", encoding=ENC, newline="") as f:
        reader = csv.reader(f)
        rows = list(reader)
    return rows[0], rows[1:]


def summarize(name: str, header: list[str], rows: list[list[str]]) -> None:
    print(f"\n{'='*70}\n{name}: {len(rows)} rows × {len(header)} cols")
    print(f"  file: {FILES[name].name}")
    # show first few header names
    show = header[:6] + (["..."] if len(header) > 6 else [])
    print(f"  cols (head): {show}")


def main() -> None:
    data = {name: load_csv(path) for name, path in FILES.items()}

    for name, (header, rows) in data.items():
        summarize(name, header, rows)

    # --- Build master SKU set from item_sku ---
    sku_header, sku_rows = data["master_skus"]
    master_code_idx = sku_header.index("商品コード")
    master_sku_idx = sku_header.index("商品SKUコード")
    master_skus: dict[str, str] = {}  # variant SKU -> parent master code
    for r in sku_rows:
        if len(r) > master_sku_idx and r[master_sku_idx]:
            master_skus[r[master_sku_idx]] = r[master_code_idx]
    print(f"\nMaster variant SKUs: {len(master_skus)} (e.g. {list(master_skus.items())[:3]})")

    # --- Master products from item_*.csv ---
    mp_header, mp_rows = data["master_products"]
    mp_code_idx = mp_header.index("商品コード")
    master_products = {r[mp_code_idx] for r in mp_rows if len(r) > mp_code_idx}
    print(f"Master products: {len(master_products)} (e.g. {sorted(master_products)[:5]})")

    # --- Rakuten: 商品管理番号 + SKU管理番号 + システム連携用SKU番号 ---
    rk_header, rk_rows = data["rakuten"]
    rk_mgmt_idx = rk_header.index("商品管理番号（商品URL）")
    rk_sku_mgmt_idx = rk_header.index("SKU管理番号")
    rk_sys_sku_idx = rk_header.index("システム連携用SKU番号")
    rk_name_idx = rk_header.index("商品名")
    # Build mgmt -> product name (taken from the parent row that has a 商品名)
    rk_names: dict[str, str] = {}
    for r in rk_rows:
        if len(r) <= max(rk_mgmt_idx, rk_name_idx):
            continue
        if r[rk_mgmt_idx] and r[rk_name_idx]:
            rk_names.setdefault(r[rk_mgmt_idx], r[rk_name_idx])
    print(f"\nRakuten total rows (incl. parent + sku detail): {len(rk_rows)}")
    rk_mgmt_set = {r[rk_mgmt_idx] for r in rk_rows if len(r) > rk_mgmt_idx and r[rk_mgmt_idx]}
    print(f"Rakuten unique 商品管理番号: {len(rk_mgmt_set)}")
    rk_sys_sku_filled = [r[rk_sys_sku_idx] for r in rk_rows
                         if len(r) > rk_sys_sku_idx and r[rk_sys_sku_idx]]
    print(f"Rakuten rows with システム連携用SKU番号 filled: {len(rk_sys_sku_filled)} "
          f"(sample: {rk_sys_sku_filled[:5]})")

    # build Rakuten mapping: 商品管理番号 -> set of (SKU管理番号, システム連携用SKU番号)
    rk_map: dict[str, list[tuple[str, str]]] = {}
    for r in rk_rows:
        if len(r) <= max(rk_mgmt_idx, rk_sku_mgmt_idx, rk_sys_sku_idx):
            continue
        mgmt = r[rk_mgmt_idx]
        sku_mgmt = r[rk_sku_mgmt_idx]
        sys_sku = r[rk_sys_sku_idx]
        if mgmt and (sku_mgmt or sys_sku):
            rk_map.setdefault(mgmt, []).append((sku_mgmt, sys_sku))

    print(f"\nRakuten 商品管理番号 with SKU child rows: {len(rk_map)}")
    print("Sample mgmt → (SKU管理番号, sys_sku):")
    for k in list(rk_map)[:5]:
        print(f"  {k} → {rk_map[k][:3]}")

    # --- Load our unmapped alerts ---
    unmapped_path = Path("/c/Users/Administrator/Desktop/master_mapping_template.csv")
    if not unmapped_path.exists():
        # fall back to discovering it
        unmapped_path = Path("C:/Users/Administrator/Desktop/master_mapping_template.csv")
    if unmapped_path.exists():
        with unmapped_path.open(encoding="utf-8") as f:
            reader = csv.reader(f)
            next(reader)  # header
            alerts = [(row[0], row[1]) for row in reader if len(row) >= 2]
    else:
        alerts = []
    print(f"\nUnmapped alerts loaded: {len(alerts)}")
    by_channel: Counter[str] = Counter(c for c, _ in alerts)
    print(f"  by channel: {dict(by_channel)}")

    # --- Try to auto-resolve alerts ---
    # Shopify: channel_sku is the variant SKU directly = 商品SKUコード in master_skus
    sh_alerts = [s for c, s in alerts if c == "shopify"]
    sh_resolved = sum(1 for s in sh_alerts if s in master_skus)
    print(f"\nShopify alerts: {len(sh_alerts)}, resolvable via master_skus exact match: {sh_resolved}")
    sh_unresolved = [s for s in sh_alerts if s not in master_skus]
    print(f"  Unresolved samples: {sh_unresolved[:10]}")

    # Rakuten: channel_sku is the 商品管理番号. Need to map to master via SKU管理番号
    # Strategy: for each Rakuten 商品管理番号 alert, look it up in rk_map, then try
    # to derive a single master code from the SKU管理番号 children.
    rk_alerts = [s for c, s in alerts if c == "rakuten"]
    rk_resolved: list[tuple[str, str, str]] = []  # (alert, master, strategy)
    rk_unresolved: list[tuple[str, str]] = []

    def try_resolve(ch_sku: str) -> tuple[str | None, str]:
        children = rk_map.get(ch_sku, [])

        # Strategy A: SKU管理番号 or システム連携用SKU番号 is directly a master variant SKU
        masters: set[str] = set()
        for sku_mgmt, sys_sku in children:
            if sku_mgmt and sku_mgmt in master_skus:
                masters.add(master_skus[sku_mgmt])
            if sys_sku and sys_sku in master_skus:
                masters.add(master_skus[sys_sku])
        if len(masters) == 1:
            return masters.pop(), "A:sku→variant"

        # Strategy B: token in SKU管理番号 (split by - or _) matches a master product code
        derived: set[str] = set()
        for sku_mgmt, _ in children:
            if sku_mgmt:
                for t in sku_mgmt.replace("_", "-").split("-"):
                    if t in master_products:
                        derived.add(t)
        if len(derived) == 1:
            return derived.pop(), "B:token→product"

        # Strategy C: alert (商品管理番号) directly matches a master product code
        if ch_sku in master_products:
            return ch_sku, "C:alert→product"

        # Strategy D: alert + "c" suffix matches (compass-style naming)
        if (ch_sku + "c") in master_products:
            return ch_sku + "c", "D:alert+c→product"

        # Strategy E: alert with leading zeros stripped + "c" suffix
        stripped = ch_sku.lstrip("0") or "0"
        if (stripped + "c") in master_products:
            return stripped + "c", "E:strip0+c"

        # Strategy F: extract "#XXX" token from Rakuten product name
        # e.g. "316L oval chain bracelet #B43" -> B43
        import re as _re
        name = rk_names.get(ch_sku, "")
        if name:
            hits = {m.group(1) for m in _re.finditer(r"#([A-Za-z]+\d+[a-z]?)", name)
                    if m.group(1) in master_products}
            if len(hits) == 1:
                return hits.pop(), "F:name#token"

        return None, "no_match"

    for ch_sku in rk_alerts:
        master, strategy = try_resolve(ch_sku)
        if master:
            rk_resolved.append((ch_sku, master, strategy))
        else:
            rk_unresolved.append((ch_sku, strategy))

    from collections import Counter as _C
    strategy_counts = _C(s for _, _, s in rk_resolved)
    print(f"\nRakuten alerts: {len(rk_alerts)}")
    print(f"  resolved: {len(rk_resolved)} ({strategy_counts.most_common()})")
    print(f"  unresolved: {len(rk_unresolved)}")
    print("  sample resolved:", rk_resolved[:8])
    print("  sample unresolved:", [u for u, _ in rk_unresolved][:15])

    # --- Write residual list ---
    residual_path = ROOT / "_residual_unmapped.csv"
    with residual_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["channel", "channel_sku", "reason"])
        for s in sh_alerts:
            if s not in master_skus:
                w.writerow(["shopify", s, "no_master_match"])
        for s, _ in rk_unresolved:
            w.writerow(["rakuten", s, "no_master_match"])
    total_unresolved = len(sh_alerts) - sh_resolved + len(rk_unresolved)
    print(f"\nResidual unmapped written: {residual_path}  ({total_unresolved} rows)")

    # --- Write proposed resolutions (for review) ---
    proposed_path = ROOT / "_proposed_resolutions.csv"
    with proposed_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["channel", "channel_sku", "master_sku", "strategy"])
        for s in sh_alerts:
            if s in master_skus:
                w.writerow(["shopify", s, s, "shopify:exact"])
        for ch_sku, master, strategy in rk_resolved:
            w.writerow(["rakuten", ch_sku, master, strategy])
    print(f"Proposed resolutions written: {proposed_path}  "
          f"({sh_resolved + len(rk_resolved)} rows)")


if __name__ == "__main__":
    main()
