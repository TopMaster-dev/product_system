"""Read-only: the proposed SKU-level mapping for Rakuten, built from the client's
own pair table (P2-039 の前段).

The shop lists most products twice — a men's page `X` and a women's page `Xc` —
and asked for both pages to share one stock figure. So the thing that holds
stock is not a Rakuten SKU but a PHYSICAL VARIANT, usually two SKUs. This builds
that view and says, for each variant, which of our products sells it:

    073 gold 19cm   <- 073/1435 (メンズ) + 073c/931 (レディース)   -> master

Nothing is written. The output is a proposal to read, and the two numbers that
decide whether a stock push is safe yet:

* **conflicts** — one variant whose SKUs resolve to different masters. One of
  the existing mappings is wrong, and guessing would bury it.
* **masters claimed twice** — two different variants pointing at one master.
  That is the defect that would write one quantity onto several live pages.

`--out` writes the proposal as a CSV for review. It touches nothing else.

    powershell -File scripts/run_cli.ps1 -Cli inspect_rakuten_variant_map ^
        -Args "--csv csv_file/dl-normal-item_20261015101912-1.csv"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.csv_export import UTF8_BOM, csv_body
from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import ChannelSkuMapping, InventorySnapshot, MasterSku
from app.services.rakuten_catalogue import (
    RAKUTEN,
    PhysicalVariant,
    Split,
    VariantAssignment,
    assign_masters,
    codeish_collisions,
    group_variants,
    links_from_legacy_mappings,
    links_from_orders,
    masters_claimed_twice,
    read_catalogue,
    read_pair_table,
    split_candidates,
)

log = get_logger(__name__)

DEFAULT_PAIRS = Path("csv_file/phase2/source/楽天ペア対応表-表1.csv")

CSV_HEADER = [
    "物理バリアント",
    "選択肢",
    "ページ",
    "商品管理番号",
    "SKU管理番号",
    "楽天の在庫数",
    "当システムのSKU",
    "当システムの在庫数",
    "対応づけの根拠",
    "状態",
    "重複の切り分け",
    "付け替え先の候補",
]

MAX_ROWS = 15


def state_of(assignment: VariantAssignment) -> str:
    if assignment.conflicting:
        return f"★対応づけが矛盾 ({len(assignment.conflicting)}商品)"
    return "未対応" if assignment.master_id is None else "対応済"


def variant_rows(
    assignments: list[VariantAssignment],
    on_hand: dict[int, int],
    splits: list[Split],
) -> list[list[object]]:
    """One row per Rakuten SKU, carrying its variant's verdict — so a reviewer
    can sort by 商品管理番号 and see both pages of a product together, and filter
    to the variants an existing product can take."""
    split_of = {split.variant: split for split in splits}
    rows: list[list[object]] = []
    for assignment in assignments:
        variant = assignment.variant
        state = state_of(assignment)
        split = split_of.get(variant)
        ours = on_hand.get(assignment.master_id or -1)
        for sku in sorted(variant.skus, key=lambda s: s.manage_number):
            rows.append(
                [
                    f"{variant.group}:{variant.label}",
                    variant.label,
                    "メンズ/無印" if sku.manage_number == variant.group else "レディース/c",
                    sku.manage_number,
                    sku.variant_id,
                    sku.quantity,
                    assignment.master_code or "",
                    "" if ours is None else ours,
                    assignment.via or "",
                    state,
                    split.verdict if split else "",
                    ", ".join(split.candidates) if split else "",
                ]
            )
    return rows


async def load_masters(
    session: AsyncSession,
) -> tuple[dict[str, tuple[int, str]], dict[int, int], dict[str, int]]:
    master_by_code = {
        code.strip().lower(): (master_id, code)
        for master_id, code in (
            await session.execute(select(MasterSku.id, MasterSku.sku_code).order_by(MasterSku.id))
        ).all()
    }
    snapshots = await session.execute(
        select(InventorySnapshot.master_sku_id, InventorySnapshot.on_hand_qty)
    )
    legacy_rows = await session.execute(
        select(ChannelSkuMapping.channel_sku, ChannelSkuMapping.master_sku_id)
        .where(
            ChannelSkuMapping.channel == RAKUTEN,
            ChannelSkuMapping.is_active.is_(True),
            ChannelSkuMapping.channel_variant_key.is_(None),
        )
        .order_by(ChannelSkuMapping.master_sku_id)
    )
    by_channel_sku: dict[str, int] = dict(legacy_rows.all())  # type: ignore[arg-type]
    return master_by_code, dict(snapshots.all()), by_channel_sku  # type: ignore[arg-type]


def print_grouping(
    variants: list[PhysicalVariant], skus: int, pairs: int, problems: list[str]
) -> None:
    paired = [v for v in variants if len(v.pages) > 1]
    singles = [v for v in variants if len(v.pages) == 1]
    print("\n  === 楽天SKUの対応づけ案 — 読み取りのみ、保存しません ===")
    print("\n  1. 物理バリアントへのまとめ")
    print(f"     ペア対応表 {pairs}組 / 楽天SKU {skus}件")
    print(f"     物理バリアント {len(variants)}件")
    print(f"       2ページで共有 {len(paired)}件 (メンズ・レディース両方に出ている商品)")
    print(f"       1ページのみ   {len(singles)}件")
    print_grouping_integrity(variants)
    for problem in problems[:5]:
        print(f"     ★ {problem}")


def print_grouping_integrity(variants: list[PhysicalVariant]) -> None:
    """Whether the fold is safe to push on: brackets read as specifications, and
    the invariant that one page sells one variant from one SKU."""
    kept = sorted({v.group for v in variants if v.kept_brackets})
    if kept:
        print(f"     括弧の中を仕様として扱った商品 {len(kept)}件 — {', '.join(kept)}")
        print("       (例: gold(3.6mm) と gold(5mm) は別のバリアントです)")
    doubled = [v for v in variants if v.duplicated_pages]
    if not doubled:
        print("     どのバリアントも1ページから1SKUのみ — 数量の宛先は一意です")
        return
    print(f"     ★ 同じページに2件以上のSKUが入ったバリアント {len(doubled)}件 — 要確認")
    for variant in doubled[:5]:
        pages = ", ".join(f"{s.manage_number}/{s.variant_id}" for s in variant.skus[:6])
        print(f"       {variant.group:<10}{variant.label:<22}{pages}")


def print_assignments(assignments: list[VariantAssignment]) -> None:
    resolved = [a for a in assignments if a.resolved]
    conflicts = [a for a in assignments if a.conflicting]
    unresolved = [a for a in assignments if a.master_id is None and not a.conflicting]
    print("\n  2. 当システムの商品との対応")
    print(f"     対応づけできた {len(resolved)}/{len(assignments)}件")
    by_route: dict[str, int] = {}
    for assignment in resolved:
        by_route[assignment.via] = by_route.get(assignment.via, 0) + 1
    for route, count in sorted(by_route.items(), key=lambda kv: -kv[1]):
        print(f"       {route:<28}{count:>5}件")
    print(f"     未対応 {len(unresolved)}件 — 当システムに対応する商品がありません")
    if unresolved:
        for assignment in unresolved[:MAX_ROWS]:
            variant = assignment.variant
            pages = ", ".join(s.manage_number for s in variant.skus)
            stock = "+".join(str(q) for q in variant.quantities)
            print(f"       {variant.group:<10}{variant.label:<24}{pages:<18}楽天在庫 {stock}")
        if len(unresolved) > MAX_ROWS:
            print(f"       ... ほか {len(unresolved) - MAX_ROWS}件")

    print(f"\n  3. 対応づけの矛盾 {len(conflicts)}件")
    if not conflicts:
        print("     ありません")
    else:
        print("     同じ物理バリアントのSKUが、別々の商品に紐づいています")
        for assignment in conflicts[:MAX_ROWS]:
            variant = assignment.variant
            pages = ", ".join(f"{s.manage_number}/{s.variant_id}" for s in variant.skus)
            print(f"       {variant.group:<10}{variant.label:<22}{pages}")


def print_double_claims(assignments: list[VariantAssignment]) -> None:
    shared = masters_claimed_twice(assignments)
    print(f"\n  4. 1つの商品を複数のバリアントが指している {len(shared)}件")
    if not shared:
        print("     ありません。在庫の書き出しは1対1です")
        return
    print("     ★ このまま反映すると、同じ数量が複数のSKUに書き込まれます")
    worst = sorted(shared.items(), key=lambda kv: -len(kv[1]))[:MAX_ROWS]
    for master_id, group in worst:
        code = group[0].master_code or f"master#{master_id}"
        labels = ", ".join(f"{a.variant.group}:{a.variant.label}" for a in group[:4])
        print(f"       {code:<20}{len(group)}バリアント  {labels}")


def print_splits(splits: list[Split], collisions: dict[str, list[str]]) -> None:
    """How the double claims come apart: a master that already exists per
    variant, or one that has to be created."""
    print(f"\n  5. 重複している対応づけの切り分け — 対象 {len(splits)}バリアント")
    if collisions:
        print(f"     ★ 記号を無視すると同一になる商品コード {len(collisions)}組")
        print("       どちらを指すか決められないため、付け替え候補から除外しました")
        for codes in list(collisions.values())[:5]:
            print(f"       {' / '.join(codes)}")
    if not splits:
        return
    by_verdict: dict[str, list[Split]] = {}
    for split in splits:
        by_verdict.setdefault(split.verdict, []).append(split)
    for verdict, group in sorted(by_verdict.items(), key=lambda kv: -len(kv[1])):
        print(f"     {verdict:<22}{len(group):>5}バリアント")
    for verdict, group in sorted(by_verdict.items(), key=lambda kv: -len(kv[1])):
        print(f"\n     [{verdict}]")
        for split in group[:MAX_ROWS]:
            found = ", ".join(split.candidates[:3]) or "—"
            print(
                f"       {split.variant.group:<10}{split.variant.label:<22}"
                f"現在 {split.claimed_code:<14}候補 {found}"
            )
        if len(group) > MAX_ROWS:
            print(f"       ... ほか {len(group) - MAX_ROWS}件")


def print_stock(assignments: list[VariantAssignment], on_hand: dict[int, int]) -> None:
    rows = [
        (a, on_hand.get(a.master_id or -1))
        for a in assignments
        if a.resolved and on_hand.get(a.master_id or -1) is not None
    ]
    print(f"\n  6. 在庫数の比較 (楽天とシステムの差) — 比較できた {len(rows)}バリアント")
    if not rows:
        return
    #: A paired variant has two Rakuten numbers; the push would set both to ours.
    differing = sorted(
        (
            (a, ours, max(abs(q - (ours or 0)) for q in a.variant.quantities))
            for a, ours in rows
            if any(q != ours for q in a.variant.quantities)
        ),
        key=lambda item: -item[2],
    )
    print(f"     一致 {len(rows) - len(differing)}件 / 差あり {len(differing)}件")
    negatives = [a for a, ours in rows if (ours or 0) < 0]
    if negatives:
        print(f"     ★ 当システムの在庫がマイナスのバリアント {len(negatives)}件 — 棚卸が先です")
    for assignment, ours, gap in differing[:MAX_ROWS]:
        variant = assignment.variant
        rakuten = "/".join(str(q) for q in variant.quantities)
        print(
            f"       {variant.group:<10}{variant.label:<22}"
            f"楽天 {rakuten:<10}当方 {ours:<7}最大差 {gap:+}"
        )
    if len(differing) > MAX_ROWS:
        print(f"       ... ほか {len(differing) - MAX_ROWS}件")


def write_proposal(
    out_dir: Path,
    assignments: list[VariantAssignment],
    on_hand: dict[int, int],
    splits: list[Split],
) -> Path:
    """Sync on purpose: file IO does not belong inside the async run (ASYNC240)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "rakuten_variant_map_proposal.csv"
    body = csv_body(CSV_HEADER, variant_rows(assignments, on_hand, splits))
    path.write_text(UTF8_BOM + body, encoding="utf-8")
    return path


async def run(*, csv_path: Path, pairs_path: Path, out_dir: Path | None) -> int:
    skus, problems = read_catalogue(csv_path)
    pairs, pair_problems = read_pair_table(pairs_path)
    if not skus:
        for problem in problems:
            print(f"  ★ {problem}")
        return 1
    if not pairs:
        for problem in pair_problems:
            print(f"  ★ {problem}")
        return 1

    variants = group_variants(skus, pairs)
    async with async_session_factory() as session:
        proven = await links_from_orders(session)
        master_by_code, on_hand, by_channel_sku = await load_masters(session)
    assignments = assign_masters(
        variants,
        proven=proven,
        master_by_code=master_by_code,
        legacy=links_from_legacy_mappings(skus, by_channel_sku),
    )

    master_codes = [c for _, c in master_by_code.values()]
    splits = split_candidates(assignments, master_codes=master_codes)

    print_grouping(variants, len(skus), len(pairs), problems + pair_problems)
    print_assignments(assignments)
    print_double_claims(assignments)
    print_splits(splits, codeish_collisions(master_codes))
    print_stock(assignments, on_hand)

    if out_dir is not None:
        print(f"\n  対応づけ案 -> {write_proposal(out_dir, assignments, on_hand, splits)}")

    resolved = sum(1 for a in assignments if a.resolved)
    log.info(
        "rakuten_variant_map.done",
        skus=len(skus),
        variants=len(variants),
        resolved=resolved,
        conflicts=sum(1 for a in assignments if a.conflicting),
        double_claimed=len(masters_claimed_twice(assignments)),
        repointable=sum(1 for s in splits if len(s.candidates) == 1),
        need_new_master=sum(1 for s in splits if not s.candidates),
    )
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Read-only proposal for the Rakuten SKU mapping")
    p.add_argument("--csv", required=True, help="dl-normal-item CSV exported from RMS")
    p.add_argument("--pairs", default=str(DEFAULT_PAIRS), help="client's page pair table")
    p.add_argument("--out", default=None, help="directory to write the proposal CSV into")
    args = p.parse_args()
    configure_logging("INFO")
    sys.exit(
        asyncio.run(
            run(
                csv_path=Path(args.csv),
                pairs_path=Path(args.pairs),
                out_dir=Path(args.out) if args.out else None,
            )
        )
    )


if __name__ == "__main__":
    main()
