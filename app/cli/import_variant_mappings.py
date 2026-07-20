"""Variant-level master/mapping importer for the bundle & shared-stock build.

FULL variant-level granularity (Phase 1-B): each master_sku is a VARIANT
(sku_code = the resolved Shopify SKU, or the Rakuten SKU管理番号 when Shopify is
absent) — not a product as in Phase 1-A. Consumes the confirmed mapping artifacts
(all under csv_file/phase1-B/):
  - mapping_resolved.csv    : confirmed 3-channel variant rows (components + normal)
  - bundle_groups.csv       : the set parents (is_bundle) + their component tokens
  - shared_stock_groups.csv : anklet(master) / bracelet(linked, is_bundle) pairs
  - bundle_definitions.csv  : set -> component tokens

Creates variant master_skus, channel_sku_mappings (shopify: Shopify SKU;
rakuten: SKU管理番号), and bundle_components (sets + shared-stock). The pure
transformation functions are unit-tested; the DB apply is an idempotent upsert
(--dry-run computes and logs without writing).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import glob
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.cli.build_channel_mapping import (
    build_rakuten_index,
    load_crossmall,
    load_rakuten_rows,
    product_token,
)
from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import BundleComponent, ChannelSkuMapping, MasterSku

log = get_logger(__name__)
ENC = "cp932"


def canonical_sku(shopify_sku: str, rakuten_sku: str) -> str:
    """Variant identity: prefer the Shopify SKU (unique per variant), else the
    Rakuten SKU管理番号. '' if neither (row is unusable)."""
    return (shopify_sku or rakuten_sku or "").strip()


@dataclass
class MasterSpec:
    sku_code: str
    name: str
    token: str
    color: str
    size: str
    is_bundle: bool = False


@dataclass
class MappingSpec:
    channel: str
    channel_sku: str
    sku_code: str


@dataclass
class LinkSpec:
    bundle_sku_code: str
    component_sku_code: str
    quantity_per: int = 1


@dataclass
class ImportPlan:
    masters: dict[str, MasterSpec] = field(default_factory=dict)
    mappings: list[MappingSpec] = field(default_factory=list)
    links: list[LinkSpec] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


# ---------- CSV loaders (return list[dict]) ----------


def _load(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding=ENC, newline="") as f:
        return list(csv.DictReader(f))


# ---------- pure transformation ----------


def build_plan(
    mapping_rows: list[dict[str, str]],
    bundle_rows: list[dict[str, str]],
    shared_rows: list[dict[str, str]],
) -> ImportPlan:
    """Build the full import plan (masters + mappings + bundle links) from the
    confirmed CSV artifacts. Pure — no DB."""
    plan = ImportPlan()

    def add_master(sku: str, token: str, color: str, size: str, *, is_bundle: bool) -> None:
        if not sku:
            return
        existing = plan.masters.get(sku)
        if existing is None:
            name = " ".join(x for x in (token, color, size) if x) or sku
            plan.masters[sku] = MasterSpec(sku, name, token, color, size, is_bundle)
        elif is_bundle:
            existing.is_bundle = True

    def add_mapping(channel: str, channel_sku: str, sku: str) -> None:
        if channel_sku and sku:
            plan.mappings.append(MappingSpec(channel, channel_sku.strip(), sku))

    # (token,color) -> sku_code, for resolving set components by token.
    by_tc: dict[tuple[str, str], str] = {}

    # 1) components + normal sellables (mapping_resolved.csv)
    for r in mapping_rows:
        token, color, size = r["token"].strip(), r["色"].strip(), r["サイズ"].strip()
        sku = canonical_sku(r.get("Shopify_SKU", ""), r.get("楽天_SKU管理番号", ""))
        if not sku:
            continue
        add_master(sku, token, color, size, is_bundle=False)
        add_mapping("shopify", r.get("Shopify_SKU", ""), sku)
        add_mapping("rakuten", r.get("楽天_SKU管理番号", ""), sku)
        by_tc.setdefault((token, color), sku)

    # 2) shared-stock: anklet = component master, bracelet = is_bundle consuming it.
    #    Create BOTH masters + their channel mappings (the anklet may be missing
    #    from mapping_resolved if it landed in the confirm sheet).
    for r in shared_rows:
        token, color = r.get("token", "").strip(), r["色"].strip()
        anklet = canonical_sku(r.get("主_Shopify_SKU", ""), r.get("主_楽天SKU管理番号", ""))
        bracelet = canonical_sku(r.get("連動_Shopify_SKU", ""), r.get("連動_楽天SKU管理番号", ""))
        if not anklet or not bracelet:
            plan.warnings.append(f"shared {token}/{color}: missing anklet/bracelet sku")
            continue
        add_master(anklet, token, color, "anklet", is_bundle=False)
        add_mapping("shopify", r.get("主_Shopify_SKU", ""), anklet)
        add_mapping("rakuten", r.get("主_楽天SKU管理番号", ""), anklet)
        add_master(bracelet, token, color, "bracelet", is_bundle=True)
        add_mapping("shopify", r.get("連動_Shopify_SKU", ""), bracelet)
        add_mapping("rakuten", r.get("連動_楽天SKU管理番号", ""), bracelet)
        plan.links.append(LinkSpec(bracelet, anklet, 1))
        by_tc.setdefault((token, color), anklet)

    # 3) set parents (bundle_groups.csv): is_bundle master + mappings; link to
    #    components by (token,color) resolved above.
    for r in bundle_rows:
        token, color = r["bundle_token"].strip(), r["色"].strip()
        parent = canonical_sku(r.get("親_Shopify_SKU", ""), r.get("親_楽天SKU管理番号", ""))
        if not parent:
            plan.warnings.append(f"bundle {token}/{color}: parent has no channel sku")
            continue
        add_master(parent, token, color, "", is_bundle=True)
        add_mapping("shopify", r.get("親_Shopify_SKU", ""), parent)
        add_mapping("rakuten", r.get("親_楽天SKU管理番号", ""), parent)
        for comp_token in r.get("構成品トークン(;)", "").split(";"):
            comp_token = comp_token.strip()
            comp_sku = by_tc.get((comp_token, color))
            if comp_sku:
                plan.links.append(LinkSpec(parent, comp_sku, 1))
            else:
                plan.warnings.append(
                    f"bundle {token}/{color}: component {comp_token} has no master"
                )

    return plan


def crossmall_key(code: str, color: str, size: str) -> str:
    """The channel_sku for a channel='crossmall' mapping — a stable key the
    reconcile CLI reconstructs from the stock CSV's (商品コード, color, size)."""
    return f"{code}|{color}|{size}"


def code_from_crossmall_key(key: str) -> str:
    """The 商品コード encoded at the start of a crossmall channel_sku."""
    return key.split("|", 1)[0]


def build_crossmall_mappings(
    masters: dict[str, MasterSpec],
    xm_var: dict[str, list[dict[str, str]]],
    code2token: dict[str, str | None],
) -> list[MappingSpec]:
    """A channel='crossmall' mapping per CROSS MALL (商品コード, color, size)
    variant -> its NON-bundle variant master, so daily reconcile can match by a
    single stock CSV. Covers ALL alias codes (006c & N23 both -> the same
    master). is_bundle masters are skipped — their stock is derived, not
    reconciled, so their CROSS MALL rows stay unmapped (reconcile ignores them)."""
    tcs_to_sku: dict[tuple[str, str, str], str] = {}
    for m in masters.values():
        if not m.is_bundle:
            tcs_to_sku.setdefault((m.token, m.color, m.size), m.sku_code)
    out: list[MappingSpec] = []
    for code, variants in xm_var.items():
        token = code2token.get(code)
        if not token:
            continue
        for v in variants:
            sku = tcs_to_sku.get((token, v["color"], v["size"]))
            if sku:
                out.append(
                    MappingSpec("crossmall", crossmall_key(code, v["color"], v["size"]), sku)
                )
    return out


# ---------- DB apply (idempotent upsert) ----------


def dedupe_mapping_rows(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Collapse rows sharing the upsert conflict key (channel, channel_sku): a
    single INSERT ... ON CONFLICT DO UPDATE cannot touch the same target row
    twice (Postgres CardinalityViolation). Sources overlap — e.g. a variant in
    both _mapping_resolved and _client_confirm_sheet — so duplicates are normal.
    Returns (deduped, conflicts) where conflicts lists channel SKUs that pointed
    to MORE THAN ONE master (a real ambiguity; the last occurrence is kept)."""
    deduped: dict[tuple[str, str], dict[str, Any]] = {}
    conflicts: list[str] = []
    for row in rows:
        key = (row["channel"], row["channel_sku"])
        prev = deduped.get(key)
        if prev is not None and prev["master_sku_id"] != row["master_sku_id"]:
            conflicts.append(
                f"{key[0]}:{key[1]} -> masters {prev['master_sku_id']} vs {row['master_sku_id']}"
            )
        deduped[key] = row
    return list(deduped.values()), conflicts


def dedupe_link_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse bundle_component rows sharing (bundle, component) — same
    single-statement upsert constraint as the mappings."""
    deduped: dict[tuple[int, int], dict[str, Any]] = {}
    for row in rows:
        deduped[(row["bundle_master_sku_id"], row["component_master_sku_id"])] = row
    return list(deduped.values())


async def apply_plan(plan: ImportPlan, *, dry_run: bool) -> dict[str, int]:
    counts = {
        "masters": len(plan.masters),
        "mappings": len(plan.mappings),
        "links": len(plan.links),
        "warnings": len(plan.warnings),
    }
    for w in plan.warnings:
        log.warning("import_variant.warning", detail=w)
    if dry_run:
        log.info("import_variant.dry_run", **counts)
        return counts

    now = datetime.now(UTC)
    async with async_session_factory() as session, session.begin():
        # 1) upsert master_skus on sku_code
        master_rows: list[dict[str, Any]] = [
            {
                "sku_code": m.sku_code,
                "name": m.name,
                "attributes": {"token": m.token, "color": m.color, "size": m.size},
                "is_bundle": m.is_bundle,
            }
            for m in plan.masters.values()
        ]
        if master_rows:
            stmt = pg_insert(MasterSku).values(master_rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=[MasterSku.sku_code],
                set_={
                    "name": stmt.excluded.name,
                    "is_bundle": stmt.excluded.is_bundle,
                    "updated_at": now,
                },
            )
            await session.execute(stmt)

        result = await session.execute(select(MasterSku.id, MasterSku.sku_code))
        code_to_id: dict[str, int] = {code: mid for mid, code in result.all()}

        # 2) upsert channel_sku_mappings (deduped on the conflict key)
        mapping_rows = [
            {
                "master_sku_id": code_to_id[m.sku_code],
                "channel": m.channel,
                "channel_sku": m.channel_sku,
                "is_active": True,
            }
            for m in plan.mappings
            if m.sku_code in code_to_id
        ]
        mapping_rows, mapping_conflicts = dedupe_mapping_rows(mapping_rows)
        if mapping_conflicts:
            log.warning(
                "import_variant.mapping_conflicts",
                count=len(mapping_conflicts),
                samples=mapping_conflicts[:20],
            )
        if mapping_rows:
            stmt = pg_insert(ChannelSkuMapping).values(mapping_rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=["channel", "channel_sku", "marketplace_id"],
                set_={
                    "master_sku_id": stmt.excluded.master_sku_id,
                    "is_active": True,
                    "updated_at": now,
                },
            )
            await session.execute(stmt)

        # 3) upsert bundle_components
        link_rows = [
            {
                "bundle_master_sku_id": code_to_id[link.bundle_sku_code],
                "component_master_sku_id": code_to_id[link.component_sku_code],
                "quantity_per": link.quantity_per,
            }
            for link in plan.links
            if link.bundle_sku_code in code_to_id and link.component_sku_code in code_to_id
        ]
        link_rows = dedupe_link_rows(link_rows)
        if link_rows:
            stmt = pg_insert(BundleComponent).values(link_rows)
            stmt = stmt.on_conflict_do_update(
                index_elements=["bundle_master_sku_id", "component_master_sku_id"],
                set_={"quantity_per": stmt.excluded.quantity_per, "updated_at": now},
            )
            await session.execute(stmt)
        counts["mappings_written"] = len(mapping_rows)
        counts["links_written"] = len(link_rows)

    log.info("import_variant.done", **counts)
    return counts


async def run(base: Path, crossmall_base: Path, *, dry_run: bool) -> int:
    # Sellable variant rows = confirmed mapping PLUS the confirm sheet, so that a
    # bundle component that resolved on Shopify but still awaits Rakuten (e.g. N61)
    # still gets a master. Rows with no channel sku at all are skipped in build_plan.
    sellable = _load(base / "_mapping_resolved.csv") + _load(base / "_client_confirm_sheet.csv")
    plan = build_plan(
        sellable,
        _load(base / "bundle_groups.csv"),
        _load(base / "shared_stock_groups.csv"),
    )
    # crossmall mappings (alias-safe) from the raw CROSS MALL structure, so daily
    # reconcile stays single-CSV (Option B).
    prod = Path(glob.glob(str(crossmall_base / "item_[0-9]*.csv"))[0])
    skus = Path(glob.glob(str(crossmall_base / "item_sku_*.csv"))[0])
    stock = Path(glob.glob(str(crossmall_base / "stock_*.csv"))[0])
    rak = Path(glob.glob(str(crossmall_base / "dl-normal-item_*.csv"))[0])
    xm_name, xm_var, _ = load_crossmall(prod, skus, stock)
    rk = build_rakuten_index(load_rakuten_rows(rak))
    code2token = {c: product_token(c, xm_name, rk) for c in xm_var}
    plan.mappings.extend(build_crossmall_mappings(plan.masters, xm_var, code2token))
    await apply_plan(plan, dry_run=dry_run)
    return 0


def main() -> None:
    p = argparse.ArgumentParser(
        description="Import variant-level masters/mappings + bundle_components"
    )
    p.add_argument(
        "--base", type=Path, default=Path("csv_file/phase1-B"), help="dir with the CSV artifacts"
    )
    p.add_argument(
        "--crossmall-base",
        type=Path,
        default=Path("csv_file/phase1-B/latest_version"),
        dest="crossmall_base",
        help="dir with raw CROSS MALL item/item_sku/stock/Rakuten (for crossmall mappings)",
    )
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    configure_logging("INFO")
    sys.exit(asyncio.run(run(args.base, args.crossmall_base, dry_run=args.dry_run)))


if __name__ == "__main__":
    main()
