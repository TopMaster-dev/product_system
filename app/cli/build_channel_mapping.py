"""Build the variant-level channel 対応表 (CROSS MALL <-> Rakuten <-> Shopify).

Phase 1-B. Resolves each CROSS MALL variant to its REAL Shopify SKU — looked up
from the live Shopify variant list, never constructed, because Shopify SKU
formats are too inconsistent to derive (B03-gold vs B48gold vs R63us7 vs
R26freegold). Rakuten is joined by direct code (商品コード == 商品管理番号) or by
the 商品番号 token at the end of the product name, then down to SKU管理番号 by
color/size.

The matching policy follows 馬渡様's guidance:
  1. parent match by the 商品番号 token (商品名 末尾)
  2. variant match by color / size / SKU管理番号
Mens/ladies pairs that share a token are unified into one product.

Inputs:
  --products  CROSS MALL 商品情報 CSV (item_*.csv): 商品コード, 商品名
  --skus      CROSS MALL 商品SKU CSV (item_sku_*.csv): 商品コード, 商品SKUコード, 属性
  --rakuten   CROSS MALL 楽天商品 CSV (normal-item_*.csv): 商品管理番号, 商品名, SKU管理番号, 選択肢
  --stock     CROSS MALL 在庫 CSV (stock_*.csv): 商品コード, 属性, 在庫数量
  --shopify-list  Live Shopify variants, tab-separated lines
                  (sku<TAB>product_title<TAB>variant_title<TAB>inventory_item_id),
                  produced by `verify_shopify_meta.py --mode=list` and exported via
                  `gcloud logging read --format='value(...)'`.

Outputs (in --out-dir): mapping_resolved.csv (confirmed 3-channel rows) and
client_confirm_sheet.csv (rows needing 馬渡様's confirmation).
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

CROSSMALL_ENCODING = "cp932"
# Jewelry-category token: a single category letter + 2-3 digits (+ opt letter).
# Excludes the 's925' material false-token.
TOKEN_RE = re.compile(r"[BNRPEAH]\d{2,3}[A-Za-z]?", re.IGNORECASE)


# ---------- pure helpers ----------


def extract_token(name: str | None) -> str | None:
    """The 商品番号 is the trailing token of the 商品名, with or without '#'."""
    if not name:
        return None
    for part in reversed(re.split(r"[\s　#＃]+", name.strip())):  # noqa: RUF001
        if TOKEN_RE.fullmatch(part):
            return part.upper()
    return None


def extract_color(*texts: str) -> str:
    blob = " ".join(t for t in texts if t).lower()
    if "gold" in blob:
        return "gold"
    if "silver" in blob:
        return "silver"
    return ""


def extract_size(*texts: str) -> str:
    blob = " ".join(t for t in texts if t)
    m = re.search(r"us[a]?\s*(\d+)", blob, re.IGNORECASE)  # US7 / USA7
    if m:
        return m.group(1)
    m = re.search(r"(\d+)\s*cm", blob, re.IGNORECASE)
    if m:
        return m.group(1)
    m = re.search(r"(?<![A-Za-z])([SML])(?![A-Za-z])", blob)
    if m:
        return m.group(1).upper()
    return ""


# ---------- channel indexes ----------


@dataclass
class ShopifyIndex:
    lookup: dict[tuple[str, str, str], str] = field(default_factory=dict)
    by_token: dict[str, list[dict[str, str]]] = field(default_factory=lambda: defaultdict(list))
    empty_sku: list[tuple[str, str]] = field(default_factory=list)
    total: int = 0


def build_shopify_index(rows: list[tuple[str, str, str]]) -> ShopifyIndex:
    """rows: (sku, product_title, variant_title)."""
    idx = ShopifyIndex()
    for sku, ptitle, vtitle in rows:
        idx.total += 1
        token = extract_token(ptitle)
        if not token:
            continue
        color = extract_color(vtitle, sku)
        size = extract_size(vtitle, sku)
        idx.by_token[token].append({"sku": sku, "color": color, "size": size})
        if sku:
            idx.lookup.setdefault((token, color, size), sku)
        else:
            idx.empty_sku.append((token, ptitle))
    return idx


def resolve_shopify(idx: ShopifyIndex, token: str, color: str, size: str) -> str:
    """Look up the real Shopify SKU, with fallbacks for color/size tagging
    differences between CROSS MALL and Shopify."""
    if (token, color, size) in idx.lookup:
        return idx.lookup[(token, color, size)]
    if color and (token, color, "") in idx.lookup:
        return idx.lookup[(token, color, "")]
    cands = [v for v in idx.by_token.get(token, []) if v["sku"]]
    if color:
        same_color = [v for v in cands if v["color"] == color]
        if len(same_color) == 1:
            return same_color[0]["sku"]
        if size:
            same = [v for v in same_color if v["size"] == size]
            if len(same) == 1:
                return same[0]["sku"]
    if len(cands) == 1:  # token has a single variant — unambiguous
        return cands[0]["sku"]
    return ""


@dataclass
class RakutenIndex:
    manage: set[str] = field(default_factory=set)
    var: dict[str, list[dict[str, str]]] = field(default_factory=lambda: defaultdict(list))
    token2manage: dict[str, str] = field(default_factory=dict)
    manage_token: dict[str, str] = field(default_factory=dict)


def build_rakuten_index(rows: list[dict[str, str]]) -> RakutenIndex:
    """rows: dicts with manage, name, sku_mgmt, opt1, opt2 (parent rows have a
    name and no sku_mgmt; child rows carry sku_mgmt + variation options)."""
    idx = RakutenIndex()
    for r in rows:
        manage = r.get("manage", "")
        if not manage:
            continue
        idx.manage.add(manage)
        if r.get("name", "").strip() and not r.get("sku_mgmt", "").strip():
            token = extract_token(r["name"])
            if token:
                idx.token2manage.setdefault(token, manage)
                idx.manage_token[manage] = token
        if r.get("sku_mgmt", "").strip():
            opt1, opt2 = r.get("opt1", ""), r.get("opt2", "")
            # Rakuten doesn't keep color/size in a fixed option column (some rows
            # carry 'gold' in opt2 and 'USA7号' in opt1), so scan both.
            idx.var[manage].append(
                {
                    "sku_mgmt": r["sku_mgmt"],
                    "color": extract_color(opt1, opt2),
                    "size": extract_size(opt2, opt1),
                }
            )
    return idx


def resolve_rakuten(idx: RakutenIndex, manage: str | None, color: str, size: str) -> str:
    if not manage:
        return ""
    cands = [v for v in idx.var.get(manage, []) if v["color"] == color]
    if size and len(cands) > 1:
        cands = [v for v in cands if v["size"] == size] or cands
    return cands[0]["sku_mgmt"] if len(cands) == 1 else ""


# ---------- the mapper ----------


def product_token(code: str, xm_name: dict[str, str], rk: RakutenIndex) -> str | None:
    # c-products lack the token in their CROSS MALL name but match a Rakuten
    # manage directly, whose name carries it; prefer that.
    if code in rk.manage_token:
        return rk.manage_token[code]
    return extract_token(xm_name.get(code, ""))


def build_mapping(
    *,
    xm_name: dict[str, str],
    xm_var: dict[str, list[dict[str, str]]],
    stock_map: dict[tuple[str, str, str], int],
    rk: RakutenIndex,
    shop: ShopifyIndex,
) -> tuple[list[list[object]], list[list[object]], dict[str, int]]:
    """Returns (mapping_rows, confirm_rows, stats)."""
    groups: dict[str, list[str]] = defaultdict(list)
    no_token: list[str] = []
    for code in xm_var:
        token = product_token(code, xm_name, rk)
        if token:
            groups[token].append(code)
        else:
            no_token.append(code)

    mapping: list[list[object]] = []
    confirm: list[list[object]] = []
    stats = {"full": 0, "shopify_only": 0, "rakuten_only": 0, "neither": 0}

    for token, members in groups.items():
        direct = [c for c in members if c in rk.manage]
        manage = direct[0] if direct else rk.token2manage.get(token)
        seen: dict[tuple[str, str], dict[str, object]] = {}
        for code in members:
            for v in xm_var[code]:
                key = (v["color"], v["size"])
                seen.setdefault(key, {"src": code, **v})
                q = stock_map.get((code, v["color"], v["size"]))
                if q is not None:
                    seen[key]["qty"] = q
        for (color, size), info in seen.items():
            shop_sku = resolve_shopify(shop, token, color, size)
            rk_sku = resolve_rakuten(rk, manage, color, size)
            qty = info.get("qty", "")
            row: list[object] = [
                token,
                info["src"],
                color,
                size,
                shop_sku,
                manage or "",
                rk_sku,
                qty,
            ]
            if shop_sku and rk_sku:
                stats["full"] += 1
                mapping.append(row)
                continue
            reasons = []
            if not shop_sku:
                reasons.append("Shopify該当SKU無し")
            if not rk_sku:
                reasons.append("楽天SKU未確定")
            confirm.append([*row, "; ".join(reasons)])
            if shop_sku:
                stats["shopify_only"] += 1
            elif rk_sku:
                stats["rakuten_only"] += 1
            else:
                stats["neither"] += 1

    for code in no_token:  # one row per product (no 商品番号 in the name)
        confirm.append(
            [
                " ",
                code,
                "",
                "",
                "",
                "",
                "",
                "",
                "トークン無し: 梱包資材/対象外か、対象ならチャネルをご記入",
            ]
        )

    stats["no_token_products"] = len(no_token)
    stats["mapping_rows"] = len(mapping)
    stats["confirm_rows"] = len(confirm)
    return mapping, confirm, stats


# ---------- IO ----------


def _read_csv(path: Path, encoding: str = CROSSMALL_ENCODING) -> list[list[str]]:
    with path.open("r", encoding=encoding, newline="") as f:
        return list(csv.reader(f))


def load_crossmall(
    products: Path, skus: Path, stock: Path
) -> tuple[dict[str, str], dict[str, list[dict[str, str]]], dict[tuple[str, str, str], int]]:
    prod = _read_csv(products)
    ph = prod[0]
    pc, pn = ph.index("商品コード"), ph.index("商品名")
    xm_name = {r[pc]: r[pn] for r in prod[1:] if len(r) > max(pc, pn) and r[pc]}

    sk = _read_csv(skus)
    sh = sk[0]
    sc, sv = sh.index("商品コード"), sh.index("商品SKUコード")
    a1, a2 = sh.index("属性１名"), sh.index("属性２名")
    xm_var: dict[str, list[dict[str, str]]] = defaultdict(list)
    for r in sk[1:]:
        if len(r) > sv and r[sv]:
            xm_var[r[sc]].append(
                {
                    "sku": r[sv],
                    "color": extract_color(r[a1], r[a2]),
                    "size": extract_size(r[a2], r[a1]),
                }
            )

    st = _read_csv(stock)
    th = st[0]
    tc, t1, t2, tq = (
        th.index("商品コード"),
        th.index("属性１名"),
        th.index("属性２名"),
        th.index("在庫数量"),
    )
    stock_map: dict[tuple[str, str, str], int] = {}
    for r in st[1:]:
        if len(r) > tq and r[tc]:
            try:
                key = (r[tc], extract_color(r[t1], r[t2]), extract_size(r[t2], r[t1]))
                stock_map[key] = int(r[tq])
            except ValueError:
                pass
    return xm_name, dict(xm_var), stock_map


def load_rakuten_rows(path: Path) -> list[dict[str, str]]:
    rk = _read_csv(path)
    h = rk[0]
    mi, nm, sm = (
        h.index("商品管理番号（商品URL）"),  # noqa: RUF001
        h.index("商品名"),
        h.index("SKU管理番号"),
    )
    o1, o2 = h.index("バリエーション項目選択肢1"), h.index("バリエーション項目選択肢2")
    out: list[dict[str, str]] = []
    for r in rk[1:]:
        if len(r) <= max(mi, nm, sm, o1, o2):
            continue
        out.append(
            {"manage": r[mi], "name": r[nm], "sku_mgmt": r[sm], "opt1": r[o1], "opt2": r[o2]}
        )
    return out


def load_shopify_list(path: Path) -> list[tuple[str, str, str]]:
    """Tab-separated lines: sku<TAB>product_title<TAB>variant_title<TAB>item_id."""
    out: list[tuple[str, str, str]] = []
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 3:
                continue
            out.append((parts[0], parts[1], parts[2]))
    return out


def _write_csv(path: Path, header: list[str], rows: list[list[object]]) -> None:
    with path.open("w", encoding=CROSSMALL_ENCODING, newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="build_channel_mapping",
        description="Build the variant-level CROSS MALL/Rakuten/Shopify 対応表.",
    )
    p.add_argument("--products", required=True, type=Path)
    p.add_argument("--skus", required=True, type=Path)
    p.add_argument("--rakuten", required=True, type=Path)
    p.add_argument("--stock", required=True, type=Path)
    p.add_argument("--shopify-list", required=True, type=Path, dest="shopify_list")
    p.add_argument("--out-dir", required=True, type=Path, dest="out_dir")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    xm_name, xm_var, stock_map = load_crossmall(args.products, args.skus, args.stock)
    rk = build_rakuten_index(load_rakuten_rows(args.rakuten))
    shop = build_shopify_index(load_shopify_list(args.shopify_list))
    mapping, confirm, stats = build_mapping(
        xm_name=xm_name, xm_var=xm_var, stock_map=stock_map, rk=rk, shop=shop
    )
    args.out_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(
        args.out_dir / "mapping_resolved.csv",
        [
            "token",
            "元商品コード",
            "色",
            "サイズ",
            "Shopify_SKU",
            "楽天_商品管理番号",
            "楽天_SKU管理番号",
            "在庫数量",
        ],
        mapping,
    )
    _write_csv(
        args.out_dir / "client_confirm_sheet.csv",
        [
            "token",
            "商品コード",
            "色",
            "サイズ",
            "Shopify_SKU",
            "楽天_商品管理番号",
            "楽天_SKU管理番号",
            "在庫数量",
            "確認依頼",
        ],
        confirm,
    )
    sys.stdout.write(
        f"mapping={stats['mapping_rows']} confirm={stats['confirm_rows']} "
        f"full={stats['full']} shopify_only={stats['shopify_only']} "
        f"rakuten_only={stats['rakuten_only']} neither={stats['neither']} "
        f"no_token_products={stats['no_token_products']} "
        f"shopify_empty_sku={len(shop.empty_sku)}\n"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
