"""One-off importer: load master products + channel SKU mappings from
CROSS MALL CSV exports, then auto-resolve all open mapping alerts.

Usage:
    py -m app.cli.import_mappings \\
        --products csv_file/sku/item_0601004857_000419.csv \\
        --skus csv_file/sku/item_sku_0601014313_000409.csv \\
        --rakuten csv_file/rakuten/normal-item_0601005523_405.csv \\
        [--manual csv_file/_manual_overrides.csv] \\
        [--dry-run]

Granularity choice (Phase 1-A):
- master_skus.sku_code = CROSS MALL 商品コード (PRODUCT-level, 405 entries)
- Shopify channel_sku  = master variant 商品SKUコード → maps to parent 商品コード
- Rakuten channel_sku  = Rakuten 商品管理番号             → maps via heuristics + manual

CSV files are CP932/Shift-JIS; we decode explicitly.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import re
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import ChannelSkuMapping, MappingAlert, MasterSku

log = get_logger(__name__)
ENC = "cp932"
NAME_TOKEN_RE = re.compile(r"[#＃]([A-Za-z]+\d+[a-z]?)")  # noqa: RUF001 — full-width sign in regex is intentional


def _read_csv(path: Path) -> tuple[list[str], list[list[str]]]:
    with path.open("r", encoding=ENC, newline="") as f:
        rows = list(csv.reader(f))
    return rows[0], rows[1:]


def _read_csv_utf8(path: Path) -> tuple[list[str], list[list[str]]]:
    with path.open("r", encoding="utf-8", newline="") as f:
        rows = list(csv.reader(f))
    return rows[0], rows[1:]


def build_master_products(products_path: Path) -> list[dict[str, Any]]:
    header, rows = _read_csv(products_path)
    code_idx = header.index("商品コード")
    name_idx = header.index("商品名")
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        if len(r) <= max(code_idx, name_idx):
            continue
        code = r[code_idx]
        if not code:
            continue
        # Names occasionally repeat across rows; first non-empty wins.
        name = r[name_idx] or code
        out.setdefault(code, {"sku_code": code, "name": name, "attributes": {}})
    return list(out.values())


def build_mappings(
    skus_path: Path,
    rakuten_path: Path,
    manual_path: Path | None,
    valid_master_codes: set[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """Return (shopify_rows, rakuten_rows, strategy_counts).

    Each row is dict(channel_sku=..., master_code=...) — master_code will be
    swapped for master_sku_id later, after master_skus are upserted.
    """
    # ---------- master variants ----------
    sku_header, sku_rows = _read_csv(skus_path)
    sku_master_idx = sku_header.index("商品コード")
    sku_variant_idx = sku_header.index("商品SKUコード")
    master_variants: dict[str, str] = {}
    for r in sku_rows:
        if len(r) > sku_variant_idx and r[sku_variant_idx]:
            master_variants[r[sku_variant_idx]] = r[sku_master_idx]

    # ---------- manual overrides (channel, channel_sku) -> master_code ----------
    manual: dict[tuple[str, str], str] = {}
    if manual_path and manual_path.exists():
        mh, mrows = _read_csv_utf8(manual_path)
        ch_i = mh.index("channel")
        cs_i = mh.index("channel_sku")
        ms_i = mh.index("master_sku")
        for r in mrows:
            if len(r) > max(ch_i, cs_i, ms_i) and r[ch_i] and r[cs_i] and r[ms_i]:
                manual[(r[ch_i], r[cs_i])] = r[ms_i]

    # ---------- shopify mappings: every master variant -> its parent product ----------
    shopify: list[dict[str, Any]] = []
    seen_shopify: set[str] = set()
    for variant_sku, master_code in master_variants.items():
        if master_code not in valid_master_codes:
            continue
        if variant_sku in seen_shopify:
            continue
        seen_shopify.add(variant_sku)
        shopify.append(
            {"channel_sku": variant_sku, "master_code": master_code, "strategy": "shopify:exact"}
        )

    # ---------- rakuten mappings ----------
    rk_header, rk_rows = _read_csv(rakuten_path)
    rk_mgmt_idx = rk_header.index("商品管理番号（商品URL）")  # noqa: RUF001 — full-width parens are in the CROSS MALL header literally
    rk_sku_mgmt_idx = rk_header.index("SKU管理番号")
    rk_sys_sku_idx = rk_header.index("システム連携用SKU番号")
    rk_name_idx = rk_header.index("商品名")

    rk_children: dict[str, list[tuple[str, str]]] = {}
    rk_names: dict[str, str] = {}
    rk_unique: set[str] = set()
    for r in rk_rows:
        if len(r) <= max(rk_mgmt_idx, rk_sku_mgmt_idx, rk_sys_sku_idx, rk_name_idx):
            continue
        m = r[rk_mgmt_idx]
        if not m:
            continue
        rk_unique.add(m)
        if r[rk_name_idx]:
            rk_names.setdefault(m, r[rk_name_idx])
        if r[rk_sku_mgmt_idx] or r[rk_sys_sku_idx]:
            rk_children.setdefault(m, []).append((r[rk_sku_mgmt_idx], r[rk_sys_sku_idx]))

    def resolve_rakuten(mgmt: str) -> tuple[str | None, str]:
        if ("rakuten", mgmt) in manual:
            return manual[("rakuten", mgmt)], "M:manual"
        children = rk_children.get(mgmt, [])
        masters: set[str] = set()
        for sku_mgmt, sys_sku in children:
            if sku_mgmt and sku_mgmt in master_variants:
                masters.add(master_variants[sku_mgmt])
            if sys_sku and sys_sku in master_variants:
                masters.add(master_variants[sys_sku])
        if len(masters) == 1:
            return masters.pop(), "A:sku→variant"
        derived: set[str] = set()
        for sku_mgmt, _ in children:
            if sku_mgmt:
                for t in sku_mgmt.replace("_", "-").split("-"):
                    if t in valid_master_codes:
                        derived.add(t)
        if len(derived) == 1:
            return derived.pop(), "B:token→product"
        if mgmt in valid_master_codes:
            return mgmt, "C:alert→product"
        if (mgmt + "c") in valid_master_codes:
            return mgmt + "c", "D:alert+c→product"
        stripped = mgmt.lstrip("0") or "0"
        if (stripped + "c") in valid_master_codes:
            return stripped + "c", "E:strip0+c"
        name = rk_names.get(mgmt, "")
        if name:
            hits = {
                m.group(1) for m in NAME_TOKEN_RE.finditer(name) if m.group(1) in valid_master_codes
            }
            if len(hits) == 1:
                return hits.pop(), "F:name#token"
        return None, "no_match"

    rakuten: list[dict[str, Any]] = []
    strategy_counts: Counter[str] = Counter()
    residual: list[str] = []
    for mgmt in sorted(rk_unique):
        master, strategy = resolve_rakuten(mgmt)
        strategy_counts[strategy] += 1
        if master:
            rakuten.append({"channel_sku": mgmt, "master_code": master, "strategy": strategy})
        else:
            residual.append(mgmt)

    # Apply manual overrides that target SKUs not in CROSS MALL (shouldn't normally happen,
    # but supports custom additions).
    for (channel, channel_sku), master_code in manual.items():
        if master_code not in valid_master_codes:
            log.warning(
                "import.manual_skip",
                channel=channel,
                channel_sku=channel_sku,
                reason="master_not_in_products",
            )
            continue
        if channel == "rakuten" and channel_sku not in {r["channel_sku"] for r in rakuten}:
            rakuten.append(
                {
                    "channel_sku": channel_sku,
                    "master_code": master_code,
                    "strategy": "M:manual_extra",
                }
            )
            strategy_counts["M:manual_extra"] += 1
        elif channel == "shopify" and channel_sku not in seen_shopify:
            shopify.append(
                {
                    "channel_sku": channel_sku,
                    "master_code": master_code,
                    "strategy": "M:manual_extra",
                }
            )
            seen_shopify.add(channel_sku)
            strategy_counts["M:manual_extra:shopify"] += 1

    if residual:
        log.warning("import.rakuten_residual", count=len(residual), samples=residual[:5])

    return shopify, rakuten, dict(strategy_counts)


async def run(
    products_path: Path,
    skus_path: Path,
    rakuten_path: Path,
    manual_path: Path | None,
    *,
    dry_run: bool = False,
) -> int:
    log.info(
        "import.start",
        products=str(products_path),
        skus=str(skus_path),
        rakuten=str(rakuten_path),
        manual=str(manual_path) if manual_path else None,
        dry_run=dry_run,
    )

    # ---- Build everything in memory first ----
    master_records = build_master_products(products_path)
    valid_master_codes = {m["sku_code"] for m in master_records}
    shopify, rakuten, strategy_counts = build_mappings(
        skus_path,
        rakuten_path,
        manual_path,
        valid_master_codes,
    )

    log.info(
        "import.plan",
        master_products=len(master_records),
        shopify_mappings=len(shopify),
        rakuten_mappings=len(rakuten),
        strategies=strategy_counts,
    )

    if dry_run:
        log.info("import.dry_run_complete")
        return 0

    # ---- Apply to DB in a single transaction ----
    async with async_session_factory() as session, session.begin():
        # 1) Upsert master_skus on (sku_code).
        if master_records:
            stmt = pg_insert(MasterSku).values(master_records)
            stmt = stmt.on_conflict_do_update(
                index_elements=[MasterSku.sku_code],
                set_={"name": stmt.excluded.name, "updated_at": datetime.now(UTC)},
            )
            await session.execute(stmt)

        # 2) Fetch all master_sku ids keyed by sku_code.
        result = await session.execute(select(MasterSku.id, MasterSku.sku_code))
        code_to_id: dict[str, int] = {code: mid for mid, code in result.all()}

        # 3) Build channel_sku_mappings rows.
        all_mapping_rows: list[dict[str, Any]] = []
        for row in shopify:
            mid = code_to_id.get(row["master_code"])
            if mid is None:
                continue
            all_mapping_rows.append(
                {
                    "master_sku_id": mid,
                    "channel": "shopify",
                    "channel_sku": row["channel_sku"],
                    "is_active": True,
                }
            )
        for row in rakuten:
            mid = code_to_id.get(row["master_code"])
            if mid is None:
                continue
            all_mapping_rows.append(
                {
                    "master_sku_id": mid,
                    "channel": "rakuten",
                    "channel_sku": row["channel_sku"],
                    "is_active": True,
                }
            )

        if all_mapping_rows:
            stmt = pg_insert(ChannelSkuMapping).values(all_mapping_rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=["channel", "channel_sku", "marketplace_id"],
                set_={
                    "master_sku_id": stmt.excluded.master_sku_id,
                    "is_active": True,
                    "updated_at": datetime.now(UTC),
                },
            )
            await session.execute(stmt)

        # 4) Auto-resolve open mapping_alerts whose (channel, channel_sku) now has a mapping.
        result = await session.execute(
            select(
                ChannelSkuMapping.channel,
                ChannelSkuMapping.channel_sku,
                ChannelSkuMapping.master_sku_id,
            )
        )
        mapping_lookup: dict[tuple[str, str], int] = {(ch, cs): mid for ch, cs, mid in result.all()}

        result = await session.execute(
            select(MappingAlert.id, MappingAlert.channel, MappingAlert.channel_sku).where(
                MappingAlert.status == "open"
            )
        )
        resolved_count = 0
        for alert_id, channel, channel_sku in result.all():
            mid = mapping_lookup.get((channel, channel_sku))
            if mid is None:
                continue
            await session.execute(
                update(MappingAlert)
                .where(MappingAlert.id == alert_id)
                .values(
                    status="resolved", resolved_master_sku_id=mid, resolved_at=datetime.now(UTC)
                )
            )
            resolved_count += 1

    log.info(
        "import.done",
        master_products=len(master_records),
        mappings_written=len(all_mapping_rows),
        alerts_resolved=resolved_count,
        strategies=strategy_counts,
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Import CROSS MALL master + mappings")
    parser.add_argument("--products", required=True, type=Path, help="商品情報CSV (item_*.csv)")
    parser.add_argument("--skus", required=True, type=Path, help="商品SKU CSV (item_sku_*.csv)")
    parser.add_argument(
        "--rakuten", required=True, type=Path, help="楽天商品CSV (normal-item_*.csv)"
    )
    parser.add_argument(
        "--manual",
        type=Path,
        default=None,
        help="Optional manual overrides CSV (headers: channel,channel_sku,master_sku)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Compute and log, don't write to DB")
    args = parser.parse_args()
    configure_logging("INFO")
    sys.exit(
        asyncio.run(
            run(
                args.products,
                args.skus,
                args.rakuten,
                args.manual,
                dry_run=args.dry_run,
            )
        )
    )


if __name__ == "__main__":
    main()
