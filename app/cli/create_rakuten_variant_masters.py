"""楽天のSKU単位の商品をこちらのシステムに作成する (P2-039)。既定は計画の表示のみ。

WHY THIS EXISTS, AND WHY IT IS URGENT

The shop counts stock from a sheet generated out of OUR master catalogue
(`app/services/stocktake.py` `sheet_rows`). Rakuten sells at SKU level — 419
products carry 1,107 SKUs, folding into 587 physical variants — but most of those
variants have no master of their own, so the sheet would print ONE line for a
product that has six sizes. The counted number could not be split afterwards, and
that shelf would have to be counted again.

So these masters have to exist before the shelf is counted, not before the push.

WHAT IT WRITES

Per physical variant, at most: one `master_skus` row, and one
`channel_sku_mappings` row PER RAKUTEN PAGE. Two pages is the normal case and the
point of the exercise — the men's page `X` and the women's page `Xc` both carry
the same physical thing, so both map to the ONE master and both will later be
fed the same quantity. That is the 「別ページ同一商品の在庫共有」 the client bought.

WHAT IT NEVER TOUCHES

* **Stock.** A new master gets no `inventory_snapshots` row. Stock arrives from
  the stocktake, and inventing an opening quantity here would be a guess written
  into a figure that later reaches live shop pages.
* **The existing page-level mapping rows.** Those are what resolve live Rakuten
  order lines (`app/services/ingest.py`), so they stay active and untouched. A
  page keeps its page-level row while its variants are mapped one at a time;
  `app.services.mapping.resolve_master_sku_id` is what keeps that unambiguous.
* **Anything it cannot decide.** A variant whose SKUs resolve to different
  masters, or whose code would collide, is reported and skipped. The run never
  guesses which master a live quantity should belong to.

    powershell -File scripts/run_cli.ps1 -Cli create_rakuten_variant_masters ^
        -Args "--csv csv_file/dl-normal-item_20261015101912-1.csv"
    powershell -File scripts/run_cli.ps1 -Cli create_rakuten_variant_masters -Apply ^
        -Args "--csv csv_file/dl-normal-item_20261015101912-1.csv"
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.csv_export import UTF8_BOM, csv_body
from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import ChannelSkuMapping, MasterSku
from app.services.rakuten_catalogue import (
    RAKUTEN,
    PhysicalVariant,
    Split,
    VariantAssignment,
    apply_decisions,
    assign_masters,
    group_variants,
    links_from_legacy_mappings,
    links_from_orders,
    masters_claimed_twice,
    read_catalogue,
    read_conflict_decisions,
    read_excluded_pages,
    read_pair_table,
    split_candidates,
)

log = get_logger(__name__)

DEFAULT_PAIRS = Path("csv_file/phase2/source/楽天ペア対応表-表1.csv")
DEFAULT_DECISIONS = Path("csv_file/phase2/source/rakuten_conflict_decisions.csv")
DEFAULT_EXCLUDED = Path("csv_file/phase2/source/rakuten_excluded_pages.csv")

#: Rakuten refuses a 商品管理番号 / SKU管理番号 longer than this, so a mapping we
#: could never push is not worth writing.
RAKUTEN_KEY_BYTES = 32
#: master_skus.sku_code is String(64) UNIQUE; master_skus.name is String(255).
SKU_CODE_MAX = 64
NAME_MAX = 255

ACTION_SKIP = "対応済み"
ACTION_MAP = "既存商品に対応づけ"
ACTION_CREATE = "商品を新規作成"
ACTION_BLOCKED = "★保留"
ACTION_EXCLUDED = "除外 (在庫ではありません)"

MAX_ROWS = 12

PLAN_HEADER = [
    "処理",
    "物理バリアント",
    "選択肢",
    "作成する商品コード",
    "商品名",
    "対応づける既存商品",
    "カテゴリID",
    "商品管理番号",
    "SKU管理番号",
    "楽天の在庫数",
    "備考",
]


def plan_rows(planned: list[Planned]) -> list[list[object]]:
    """The WHOLE plan, one row per Rakuten SKU.

    The screen prints twelve of each kind, which is right for reading and wrong
    for checking. 97 products and 937 mappings are about to be written to a live
    catalogue, so the reviewable artefact has to be the complete list.
    """
    rows: list[list[object]] = []
    for item in planned:
        variant = item.variant
        for sku in sorted(variant.skus, key=lambda s: s.manage_number):
            rows.append(
                [
                    item.action,
                    f"{variant.group}:{variant.label}",
                    variant.label,
                    item.sku_code,
                    item.name,
                    item.master_code,
                    "" if item.category_id is None else item.category_id,
                    sku.manage_number,
                    sku.variant_id,
                    sku.quantity,
                    item.reason,
                ]
            )
    return rows


def write_plan(out_dir: Path, planned: list[Planned]) -> Path:
    """Sync on purpose: file IO does not belong inside the async run (ASYNC240)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "rakuten_variant_master_plan.csv"
    path.write_text(UTF8_BOM + csv_body(PLAN_HEADER, plan_rows(planned)), encoding="utf-8")
    return path


@dataclass(frozen=True, slots=True)
class Planned:
    """One physical variant and what this run would do about it."""

    variant: PhysicalVariant
    action: str
    sku_code: str = ""
    name: str = ""
    #: Set when an existing master already represents this variant.
    master_id: int | None = None
    master_code: str = ""
    category_id: int | None = None
    reason: str = ""

    @property
    def new_mappings(self) -> int:
        if self.action in {ACTION_SKIP, ACTION_BLOCKED, ACTION_EXCLUDED}:
            return 0
        return len(self.variant.skus)


def _safe(text: str) -> str:
    """A choice as it can appear inside a SKU code.

    Kept deliberately close to the shop's own wording — the client reads these
    off a count sheet — but stripped of the characters that would break a CSV
    cell or a URL.
    """
    cleaned = unicodedata.normalize("NFKC", text)
    for bad in (",", '"', "'", "\\", "/", " ", "\t", "\n"):
        cleaned = cleaned.replace(bad, "")
    return cleaned.strip("-_")


def variant_sku_code(variant: PhysicalVariant) -> str:
    """`073-gold-19cm`. Deterministic, so a re-run plans the same codes.

    Over-long codes take a hash of the full identity rather than a bare
    truncation, because two sizes of one product share a long prefix and a
    truncation would silently merge them.
    """
    parts = [variant.group, *(_safe(choice) for choice in variant.choices)]
    code = "-".join(part for part in parts if part)
    if len(code) <= SKU_CODE_MAX:
        return code
    digest = hashlib.sha256(code.encode()).hexdigest()[:7]
    return f"{code[: SKU_CODE_MAX - 8]}-{digest}"


def variant_name(variant: PhysicalVariant) -> str:
    """The shop's own 商品名 plus the choice, so the count sheet is readable."""
    base = next((sku.name for sku in variant.skus if sku.name), variant.group)
    label = variant.label if variant.choices else ""
    full = f"{base} {label}".strip()
    return full[:NAME_MAX]


def _too_long_for_rakuten(variant: PhysicalVariant) -> str:
    for sku in variant.skus:
        for label, value in (("商品管理番号", sku.manage_number), ("SKU管理番号", sku.variant_id)):
            if len(value.encode()) > RAKUTEN_KEY_BYTES:
                return f"{label} が{RAKUTEN_KEY_BYTES}バイトを超えます ({value})"
    return ""


@dataclass(frozen=True, slots=True)
class CatalogueState:
    """Everything the plan needs to read out of our own database."""

    #: lowered sku_code -> (master id, as-written code)
    master_by_code: dict[str, tuple[int, str]]
    #: master id -> category
    category_of: dict[int, int | None]
    #: (商品管理番号, SKU管理番号) pairs already mapped at SKU level
    already_mapped: set[tuple[str, str]]
    #: Rakuten page -> the category its mapping's master carries
    category_by_page: dict[str, int]
    #: master ids that are retired. Kept out of code matching.
    archived: set[int]
    #: lowered sku_code of masters THIS tool created, so a re-run reuses them.
    made_here: set[str]
    #: channel_sku -> master, for Rakuten rows with no variant key. Most of these
    #: hold a SKU管理番号 rather than a page; `links_from_legacy_mappings` sorts
    #: that out using the export.
    by_channel_sku: dict[str, int]


async def load_state(session: AsyncSession) -> CatalogueState:
    """Master codes, categories, what is already mapped, and the mappings we
    already hold.

    The last of those carries most of the weight. Measured 2026-10-19, 480 of
    512 active Rakuten mappings are variant-level truth filed under the wrong
    column, and reading them is the difference between creating 52 masters and
    creating 475.
    """
    rows = await session.execute(
        select(
            MasterSku.id,
            MasterSku.sku_code,
            MasterSku.category_id,
            MasterSku.archived_at,
            MasterSku.attributes,
        ).order_by(MasterSku.id)
    )
    by_code: dict[str, tuple[int, str]] = {}
    category_of: dict[int, int | None] = {}
    archived: set[int] = set()
    made_here: set[str] = set()
    for master_id, code, category_id, archived_at, attributes in rows.all():
        by_code[code.strip().lower()] = (master_id, code)
        category_of[master_id] = category_id
        if archived_at is not None:
            archived.add(master_id)
        if (attributes or {}).get("source") == "rakuten_variant":
            made_here.add(code.strip().lower())

    mapped = await session.execute(
        select(ChannelSkuMapping.channel_sku, ChannelSkuMapping.channel_variant_key).where(
            ChannelSkuMapping.channel == RAKUTEN,
            ChannelSkuMapping.channel_variant_key.is_not(None),
        )
    )
    existing = {(page, key) for page, key in mapped.all() if key}

    page_rows = await session.execute(
        select(ChannelSkuMapping.channel_sku, MasterSku.category_id)
        .join(MasterSku, MasterSku.id == ChannelSkuMapping.master_sku_id)
        .where(
            ChannelSkuMapping.channel == RAKUTEN,
            ChannelSkuMapping.is_active.is_(True),
            ChannelSkuMapping.channel_variant_key.is_(None),
            MasterSku.category_id.is_not(None),
        )
        .order_by(MasterSku.id)
    )
    category_by_page = {page: category for page, category in page_rows.all() if category}

    # Rakuten rows that carry no variant key. Most of them hold a SKU管理番号
    # in channel_sku rather than a page; `links_from_legacy_mappings` decides
    # which, using the export.
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
    return CatalogueState(
        master_by_code=by_code,
        category_of=category_of,
        already_mapped=existing,
        archived=archived,
        made_here=made_here,
        category_by_page=category_by_page,
        by_channel_sku=by_channel_sku,
    )


def build_plan(
    variants: list[PhysicalVariant],
    *,
    proven: dict[tuple[str, str], int],
    master_by_code: dict[str, tuple[int, str]],
    category_of: dict[int, int | None],
    already_mapped: set[tuple[str, str]],
    category_by_page: dict[str, int] | None = None,
    legacy: dict[tuple[str, str], int] | None = None,
    decisions: dict[tuple[str, str], str] | None = None,
    excluded: set[str] | None = None,
    archived: set[int] | None = None,
    made_here: set[str] | None = None,
) -> list[Planned]:
    """Decide one action per variant, refusing anything ambiguous."""
    assignments = assign_masters(
        variants,
        proven=proven,
        master_by_code=master_by_code,
        legacy=legacy,
        archived=archived,
    )
    assignments, decision_problems = apply_decisions(
        assignments, decisions=decisions or {}, master_by_code=master_by_code
    )
    for problem in decision_problems:
        print(f"  ★ 判断ファイル: {problem}")

    # Drop the excluded pages BEFORE counting who claims what. An excluded page
    # is not a claimant: `10060cc` is a duplicate listing of `10060`, and while
    # it was still counted, `P07gold` looked like it was wanted by two variants,
    # so neither could take it and BOTH created a master beside the product that
    # already existed. The exclusion has to apply first for it to mean anything.
    def is_excluded(assignment: VariantAssignment) -> bool:
        variant = assignment.variant
        return bool(excluded) and (
            variant.group in (excluded or ()) or bool(set(variant.pages) & (excluded or set()))
        )

    live = [a for a in assignments if not is_excluded(a)]
    shared = masters_claimed_twice(live)
    by_group = category_by_group(live, category_of, category_by_page or {})
    codes = [code for _, code in master_by_code.values()]
    splits = {split.variant: split for split in split_candidates(live, master_codes=codes)}
    taken = {code.lower() for code in master_by_code}
    ours = {code: found for code, found in master_by_code.items() if code in (made_here or set())}

    planned: list[Planned] = []
    for assignment in assignments:
        variant = assignment.variant
        if is_excluded(assignment):
            planned.append(Planned(variant, ACTION_EXCLUDED))
            continue
        if all((sku.manage_number, sku.variant_id) in already_mapped for sku in variant.skus):
            planned.append(Planned(variant, ACTION_SKIP))
            continue

        blocked = _blocked_reason(assignment, variant, splits)
        if blocked:
            planned.append(Planned(variant, ACTION_BLOCKED, reason=blocked))
            continue

        target = _existing_master(assignment, variant, splits, shared, master_by_code)
        if target is not None:
            master_id, master_code = target
            planned.append(
                Planned(
                    variant,
                    ACTION_MAP,
                    master_id=master_id,
                    master_code=master_code,
                    category_id=category_of.get(master_id),
                )
            )
            continue

        code = variant_sku_code(variant)
        if code.lower() in taken:
            # A master WE made earlier for THIS variant is a re-run, not a
            # collision. The code is derived from the variant, so an exact match
            # on a master this tool created means the product already exists and
            # only its mapping is missing — which is the state a repair leaves
            # behind after bad mapping rows are removed. Without this the repair
            # cannot converge: every master it needs is one it already made.
            mine = ours.get(code.lower())
            if mine is not None:
                planned.append(
                    Planned(
                        variant,
                        ACTION_MAP,
                        master_id=mine[0],
                        master_code=mine[1],
                        category_id=category_of.get(mine[0]),
                    )
                )
                continue
            planned.append(
                Planned(variant, ACTION_BLOCKED, reason=f"商品コード {code} は既に使われています")
            )
            continue
        taken.add(code.lower())
        planned.append(
            Planned(
                variant,
                ACTION_CREATE,
                sku_code=code,
                name=variant_name(variant),
                # The variant must land in the SAME category as the product it
                # came from, or a category-filtered count sheet omits it and the
                # shelf gets counted while the variant does not.
                category_id=_category_for(
                    assignment, variant, category_of, category_by_page or {}, by_group
                ),
                master_code=assignment.master_code,
            )
        )
    return planned


def _direct_category(
    assignment: VariantAssignment,
    category_of: dict[int, int | None],
    category_by_page: dict[str, int],
) -> int | None:
    """This variant's own category: its master's, else its Rakuten page's."""
    claimed = category_of.get(assignment.master_id or -1)
    if claimed is not None:
        return claimed
    for page in assignment.variant.pages:
        found = category_by_page.get(page)
        if found is not None:
            return found
    return None


def category_by_group(
    assignments: list[VariantAssignment],
    category_of: dict[int, int | None],
    category_by_page: dict[str, int],
) -> dict[str, int]:
    """One category per Rakuten product, where its own variants agree on it.

    A 商品管理番号 is one product, so its sizes and colours belong in the same
    place. Where the variants that DO resolve agree, the ones that do not can
    inherit from their siblings — `073 gold / 20cm` belongs wherever
    `073 gold / 19cm` does.

    Where they disagree, nothing is inherited. Guessing would put a product on
    the wrong count sheet, and a variant missing from the sheet it should be on
    is the failure this whole exercise exists to prevent.
    """
    seen: dict[str, set[int]] = {}
    for assignment in assignments:
        found = _direct_category(assignment, category_of, category_by_page)
        if found is not None:
            seen.setdefault(assignment.variant.group, set()).add(found)
    return {group: next(iter(cats)) for group, cats in seen.items() if len(cats) == 1}


def _category_for(
    assignment: VariantAssignment,
    variant: PhysicalVariant,
    category_of: dict[int, int | None],
    category_by_page: dict[str, int],
    by_group: dict[str, int] | None = None,
) -> int | None:
    """Three steps: this variant's master, its page, then its siblings.

    The page step carries most of the weight — a variant no sale has placed has
    no claimed master, and without it 461 of 475 new masters came out 未分類
    against a catalogue that is otherwise 99% classified. The sibling step picks
    up the remainder, where a product's other sizes resolved and this one did not.
    """
    direct = _direct_category(assignment, category_of, category_by_page)
    if direct is not None:
        return direct
    return (by_group or {}).get(variant.group)


def _blocked_reason(
    assignment: VariantAssignment,
    variant: PhysicalVariant,
    splits: dict[PhysicalVariant, Split],
) -> str:
    if assignment.conflicting:
        return f"SKUが別々の商品に紐づいています ({len(assignment.conflicting)}件)"
    if variant.duplicated_pages:
        return f"同じページに複数のSKUが入っています ({', '.join(variant.duplicated_pages)})"
    split = splits.get(variant)
    if split is not None and len(split.candidates) > 1:
        return f"付け替え先の候補が複数あります ({', '.join(split.candidates)})"
    return _too_long_for_rakuten(variant)


def _existing_master(
    assignment: VariantAssignment,
    variant: PhysicalVariant,
    splits: dict[PhysicalVariant, Split],
    shared: dict[int, list[VariantAssignment]],
    master_by_code: dict[str, tuple[int, str]],
) -> tuple[int, str] | None:
    """The master that already represents THIS variant alone, if there is one."""
    split = splits.get(variant)
    if split is not None and len(split.candidates) == 1:
        found = master_by_code.get(split.candidates[0].strip().lower())
        return found if found else None
    # A resolved variant whose master no other variant claims is already 1:1.
    master_id = assignment.master_id
    if assignment.resolved and master_id is not None and master_id not in shared:
        return (master_id, assignment.master_code)
    return None


def print_plan(planned: list[Planned], variants: int) -> None:
    by_action: dict[str, list[Planned]] = {}
    for item in planned:
        by_action.setdefault(item.action, []).append(item)

    print("\n  === 楽天SKU単位の商品作成 — 計画 ===")
    print(f"\n  物理バリアント {variants}件")
    for action in (ACTION_SKIP, ACTION_MAP, ACTION_CREATE, ACTION_EXCLUDED, ACTION_BLOCKED):
        group = by_action.get(action, [])
        mappings = sum(item.new_mappings for item in group)
        extra = f" / 対応づけ {mappings}件" if mappings else ""
        print(f"    {action:<22}{len(group):>5}件{extra}")

    created = by_action.get(ACTION_CREATE, [])
    uncategorised = [item for item in created if item.category_id is None]
    if uncategorised:
        print(f"\n  ★ カテゴリが決まらない商品 {len(uncategorised)}件")
        print("     棚卸のカテゴリ別集計表に出ないため、作成後に分類してください")
        # Named, not just counted: these are the only ones a person still has to
        # decide, and a count alone leaves them to go hunting for which.
        for item in uncategorised:
            print(f"       {item.sku_code:<28}{item.name[:46]}")

    for action in (ACTION_CREATE, ACTION_MAP, ACTION_BLOCKED):
        group = by_action.get(action, [])
        if not group:
            continue
        print(f"\n  [{action}] {len(group)}件")
        for item in group[:MAX_ROWS]:
            pages = ",".join(item.variant.pages)
            if action == ACTION_BLOCKED:
                print(f"    {item.variant.group:<10}{item.variant.label:<22}{item.reason}")
            elif action == ACTION_MAP:
                print(f"    {item.variant.group:<10}{item.variant.label:<22}-> {item.master_code}")
            else:
                print(f"    {item.sku_code:<28}{pages:<16}{item.name[:40]}")
        if len(group) > MAX_ROWS:
            print(f"    ... ほか {len(group) - MAX_ROWS}件")


async def apply_plan(session: AsyncSession, planned: list[Planned]) -> tuple[int, int]:
    """Create the masters and their mappings. Writes no stock, touches no
    existing row."""
    masters = 0
    mappings = 0
    for item in planned:
        if item.action == ACTION_CREATE:
            master = MasterSku(
                sku_code=item.sku_code,
                name=item.name,
                category_id=item.category_id,
                attributes={
                    "source": "rakuten_variant",
                    "rakuten_group": item.variant.group,
                    "choices": list(item.variant.choices),
                    "from_master": item.master_code,
                },
            )
            session.add(master)
            await session.flush()
            master_id = master.id
            masters += 1
        elif item.action == ACTION_MAP:
            master_id = item.master_id or 0
        else:
            continue

        for sku in item.variant.skus:
            session.add(
                ChannelSkuMapping(
                    master_sku_id=master_id,
                    channel=RAKUTEN,
                    channel_sku=sku.manage_number,
                    channel_variant_key=sku.variant_id,
                    is_active=True,
                )
            )
            mappings += 1
    return masters, mappings


async def run(
    *,
    csv_path: Path,
    pairs_path: Path,
    decisions_path: Path,
    excluded_path: Path,
    out_dir: Path | None,
    apply_changes: bool,
) -> int:
    skus, problems = read_catalogue(csv_path)
    pairs, pair_problems = read_pair_table(pairs_path)
    if not skus or not pairs:
        for problem in problems + pair_problems:
            print(f"  ★ {problem}")
        return 1

    variants = group_variants(skus, pairs)
    decisions, decision_problems = read_conflict_decisions(decisions_path)
    excluded, exclude_problems = read_excluded_pages(excluded_path)
    decision_problems += exclude_problems
    for problem in decision_problems:
        print(f"  ★ 判断ファイル: {problem}")
    async with async_session_factory() as session:
        proven = await links_from_orders(session)
        state = await load_state(session)
        planned = build_plan(
            variants,
            proven=proven,
            master_by_code=state.master_by_code,
            category_of=state.category_of,
            already_mapped=state.already_mapped,
            category_by_page=state.category_by_page,
            legacy=links_from_legacy_mappings(skus, state.by_channel_sku),
            decisions=decisions,
            excluded=excluded,
            archived=state.archived,
            made_here=state.made_here,
        )
        print_plan(planned, len(variants))

        if out_dir is not None:
            print(f"\n  計画の全件 -> {write_plan(out_dir, planned)}")

        if not apply_changes:
            print("\n  ※ 作成していません (--apply で実行)")
            masters = mappings = 0
        else:
            # Explicit commit, not `session.begin()`: the plan above has already
            # read through this session, so SQLAlchemy has autobegun and opening
            # a transaction here raises InvalidRequestError.
            try:
                masters, mappings = await apply_plan(session, planned)
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            print(f"\n  作成しました: 商品 {masters}件 / 対応づけ {mappings}件")
            print("  在庫数は登録していません。棚卸の結果を取り込んでください")

    log.info(
        "rakuten_variant_masters.done",
        variants=len(variants),
        create=sum(1 for p in planned if p.action == ACTION_CREATE),
        map_existing=sum(1 for p in planned if p.action == ACTION_MAP),
        blocked=sum(1 for p in planned if p.action == ACTION_BLOCKED),
        applied=apply_changes,
        masters_created=masters,
        mappings_created=mappings,
    )
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Create SKU-level masters for the Rakuten catalogue")
    p.add_argument("--csv", required=True, help="dl-normal-item CSV exported from RMS")
    p.add_argument("--pairs", default=str(DEFAULT_PAIRS), help="client's page pair table")
    p.add_argument(
        "--decisions",
        default=str(DEFAULT_DECISIONS),
        help="reviewed decisions settling ambiguous variants",
    )
    p.add_argument(
        "--excluded",
        default=str(DEFAULT_EXCLUDED),
        help="商品管理番号 that must not become products of ours",
    )
    p.add_argument("--out", default=None, help="directory to write the full plan CSV into")
    p.add_argument("--apply", action="store_true", help="actually create (default: plan only)")
    args = p.parse_args()
    configure_logging("INFO")
    sys.exit(
        asyncio.run(
            run(
                csv_path=Path(args.csv),
                pairs_path=Path(args.pairs),
                decisions_path=Path(args.decisions),
                excluded_path=Path(args.excluded),
                out_dir=Path(args.out) if args.out else None,
                apply_changes=args.apply,
            )
        )
    )


if __name__ == "__main__":
    main()
