"""Build the FULL channel_sku → master_sku mapping for ALL products
in CROSS MALL (not just the 165 currently-unmapped alerts).

Outputs:
  _full_mapping.csv             — every (channel, channel_sku, master_sku, strategy) row
  _full_mapping_residual.csv    — Rakuten 商品管理番号 we still cannot resolve
"""

from __future__ import annotations

import csv
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).parent
ENC = "cp932"

# Manual mappings the client provided for the original residual 4.
MANUAL: dict[tuple[str, str], str] = {
    ("rakuten", "10102c"): "B49",
    ("rakuten", "10099"): "B46",
    ("rakuten", "10098c"): "R47",
    ("rakuten", "10080c"): "N70",
}


def load_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    with path.open("r", encoding=ENC, newline="") as f:
        rows = list(csv.reader(f))
    return rows[0], rows[1:]


def main() -> None:
    mp_header, mp_rows = load_csv(ROOT / "sku" / "item_0601004857_000419.csv")
    sku_header, sku_rows = load_csv(ROOT / "sku" / "item_sku_0601014313_000409.csv")
    rk_header, rk_rows = load_csv(ROOT / "rakuten" / "normal-item_0601005523_405.csv")

    mp_code_idx = mp_header.index("商品コード")
    master_products = {r[mp_code_idx] for r in mp_rows if len(r) > mp_code_idx and r[mp_code_idx]}

    sku_master_idx = sku_header.index("商品コード")
    sku_variant_idx = sku_header.index("商品SKUコード")
    # variant SKU -> parent master product code
    master_variants: dict[str, str] = {}
    for r in sku_rows:
        if len(r) > sku_variant_idx and r[sku_variant_idx]:
            master_variants[r[sku_variant_idx]] = r[sku_master_idx]

    print(f"Master products:  {len(master_products)}")
    print(f"Master variants:  {len(master_variants)}")

    # ---- Shopify side ----
    # Shopify channel SKU = master variant SKU directly (1:1).
    shopify_mappings: list[tuple[str, str, str]] = []
    for variant_sku, master_code in master_variants.items():
        shopify_mappings.append((variant_sku, master_code, "shopify:exact"))
    print(f"\nShopify mappings to emit: {len(shopify_mappings)}")

    # ---- Rakuten side ----
    rk_mgmt_idx = rk_header.index("商品管理番号（商品URL）")
    rk_sku_mgmt_idx = rk_header.index("SKU管理番号")
    rk_sys_sku_idx = rk_header.index("システム連携用SKU番号")
    rk_name_idx = rk_header.index("商品名")

    # mgmt -> list of (SKU管理番号, システム連携用SKU番号), and mgmt -> 商品名
    rk_children: dict[str, list[tuple[str, str]]] = {}
    rk_names: dict[str, str] = {}
    for r in rk_rows:
        if len(r) <= max(rk_mgmt_idx, rk_sku_mgmt_idx, rk_sys_sku_idx, rk_name_idx):
            continue
        m = r[rk_mgmt_idx]
        if not m:
            continue
        if r[rk_name_idx]:
            rk_names.setdefault(m, r[rk_name_idx])
        if r[rk_sku_mgmt_idx] or r[rk_sys_sku_idx]:
            rk_children.setdefault(m, []).append((r[rk_sku_mgmt_idx], r[rk_sys_sku_idx]))

    rk_unique = {r[rk_mgmt_idx] for r in rk_rows
                 if len(r) > rk_mgmt_idx and r[rk_mgmt_idx]}
    print(f"Rakuten unique 商品管理番号: {len(rk_unique)}")

    name_token_re = re.compile(r"[#＃]([A-Za-z]+\d+[a-z]?)")

    def resolve_rakuten(mgmt: str) -> tuple[str | None, str]:
        # 0) explicit manual override from the client
        if ("rakuten", mgmt) in MANUAL:
            return MANUAL[("rakuten", mgmt)], "M:manual"

        children = rk_children.get(mgmt, [])

        # A: any child SKU管理番号 or システム連携用SKU番号 is a master variant SKU
        masters = set()
        for sku_mgmt, sys_sku in children:
            if sku_mgmt and sku_mgmt in master_variants:
                masters.add(master_variants[sku_mgmt])
            if sys_sku and sys_sku in master_variants:
                masters.add(master_variants[sys_sku])
        if len(masters) == 1:
            return masters.pop(), "A:sku→variant"

        # B: token in SKU管理番号 (split by - or _) matches a master product code
        derived = set()
        for sku_mgmt, _ in children:
            if sku_mgmt:
                for t in sku_mgmt.replace("_", "-").split("-"):
                    if t in master_products:
                        derived.add(t)
        if len(derived) == 1:
            return derived.pop(), "B:token→product"

        # C: 商品管理番号 itself is a master product code
        if mgmt in master_products:
            return mgmt, "C:alert→product"

        # D: 商品管理番号 + "c" suffix matches
        if (mgmt + "c") in master_products:
            return mgmt + "c", "D:alert+c→product"

        # E: 商品管理番号 with leading zeros stripped + "c" suffix
        stripped = mgmt.lstrip("0") or "0"
        if (stripped + "c") in master_products:
            return stripped + "c", "E:strip0+c"

        # F: extract "#XXX" token from Rakuten product name
        name = rk_names.get(mgmt, "")
        if name:
            hits = {m.group(1) for m in name_token_re.finditer(name)
                    if m.group(1) in master_products}
            if len(hits) == 1:
                return hits.pop(), "F:name#token"

        # G: if all children share a common base by stripping "gold"/"silver"/size tokens
        # not implemented — let it fall through to residual
        return None, "no_match"

    rakuten_mappings: list[tuple[str, str, str]] = []
    rakuten_residual: list[tuple[str, str, str]] = []  # (mgmt, name, children_preview)
    strategy_counter: Counter[str] = Counter()
    for mgmt in sorted(rk_unique):
        master, strategy = resolve_rakuten(mgmt)
        strategy_counter[strategy] += 1
        if master:
            rakuten_mappings.append((mgmt, master, strategy))
        else:
            kids = rk_children.get(mgmt, [])
            rakuten_residual.append((
                mgmt,
                rk_names.get(mgmt, "")[:80],
                "; ".join(f"{a}|{b}" for a, b in kids[:3]),
            ))

    print(f"\nRakuten resolved: {len(rakuten_mappings)} / {len(rk_unique)} "
          f"({100 * len(rakuten_mappings) / len(rk_unique):.1f}%)")
    print(f"Strategy breakdown:")
    for s, n in strategy_counter.most_common():
        print(f"  {s}: {n}")
    print(f"\nRakuten residual: {len(rakuten_residual)}")
    for r in rakuten_residual[:15]:
        print(f"  {r[0]:25s}  name={r[1]!r}  children={r[2]}")

    # ---- Write outputs ----
    out_full = ROOT / "_full_mapping.csv"
    with out_full.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["channel", "channel_sku", "master_sku", "strategy"])
        for sku, mc, s in shopify_mappings:
            w.writerow(["shopify", sku, mc, s])
        for mgmt, mc, s in rakuten_mappings:
            w.writerow(["rakuten", mgmt, mc, s])
    total = len(shopify_mappings) + len(rakuten_mappings)
    print(f"\nWrote {out_full.name}: {total} rows "
          f"(shopify={len(shopify_mappings)}, rakuten={len(rakuten_mappings)})")

    out_resid = ROOT / "_full_mapping_residual.csv"
    with out_resid.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["channel", "channel_sku", "product_name", "sku_children_preview"])
        for mgmt, name, kids in rakuten_residual:
            w.writerow(["rakuten", mgmt, name, kids])
    print(f"Wrote {out_resid.name}: {len(rakuten_residual)} rows")


if __name__ == "__main__":
    main()
