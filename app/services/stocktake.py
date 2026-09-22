"""実地棚卸 — turning a counted sheet into an approval queue (P2-028 / P2-030).

The client counts what is physically on the shelf and sends the sheet back. This
resolves each row to a master SKU, compares it with what the system believes,
and hands the differences to the SAME approval path the CROSS MALL
reconciliation and the Shopify audit use — `ReconcileService.start_run` with
`run_type='stocktake'`. That path is the only code in the system that
deliberately overwrites a snapshot; there is one of it, and this is a third
caller rather than a third copy.

THE RULE THAT MATTERS: A SKU NOT ON THE SHEET WAS NOT COUNTED AS ZERO.

The client confirmed on 2026-09-22 that they will count in instalments, by
category. A RING day covers 265 SKUs and leaves 449 untouched. If an absent row
were read as "counted zero", the first instalment would propose emptying the
stock of every necklace, bracelet, pierce, anklet and key ring in the
catalogue — and every one of those proposals would arrive in the approval queue
looking exactly like a routine correction, because that is what the queue is
for.

So only rows PRESENT in the file are compared. What was in scope is recorded in
`scope_note` rather than inferred from what is missing.

RESOLUTION IS BY `sku_code`, NOT BY CHANNEL MAPPING.

The sheet is produced from our own master, so its identifiers are ours. Going
through `channel_sku_mappings` would drop any master with no live channel
mapping — which is exactly the retired or newly-registered stock a physical
count exists to establish.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.csv_intake import (
    ColumnSpec,
    CsvSpec,
    Inspection,
    OnEmpty,
    inspect,
    int_validator,
    iter_rows,
)
from app.models import InventorySnapshot, MasterSku, ProductCategory
from app.services.reconcile import DiffInput
from app.services.sku_scope import operational_conditions

COL_SKU = "sku_code"
COL_COUNTED = "counted_qty"

#: The count sheet, as the exporter writes it and the importer reads it back.
#:
#: Aliases are generous because the sheet goes through Excel and comes back
#: however the person who filled it in left the header. The canonical names are
#: what the exporter writes, so an untouched round trip always matches.
STOCKTAKE_CSV_SPEC = CsvSpec(
    columns=(
        ColumnSpec(
            canonical=COL_SKU,
            aliases=("SKUコード", "SKU", "商品コード"),
            required=True,
            empty_message="SKUコードが空です",
        ),
        ColumnSpec(
            canonical=COL_COUNTED,
            aliases=("実数", "実地在庫数", "カウント数", "実在庫"),
            required=True,
            # A blank count is a row the counter did not reach, NOT a zero. It
            # is skipped rather than treated as "none on the shelf", for the
            # same reason an absent row is: a partial count must not empty
            # anything.
            on_empty=OnEmpty.SKIP,
            validator=int_validator("実数が数値ではありません: '{value}'"),
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class CountedRow:
    sku_code: str
    counted_qty: int


@dataclass(slots=True)
class StocktakePlan:
    """What an upload would do, computed without touching the database."""

    counted: list[CountedRow] = field(default_factory=list)
    diffs: list[DiffInput] = field(default_factory=list)
    #: Rows whose sku_code matches no master. Reported rather than dropped —
    #: a typo in a count sheet is the operator's to fix, and silently ignoring
    #: it loses a real shelf count. Malformed rows carry line numbers in
    #: `Inspection.row_issues`; these are well-formed rows naming an unknown
    #: SKU, which the operator finds by code rather than by line.
    unknown: list[CountedRow] = field(default_factory=list)
    #: Counted and already correct. Carried so the screen can say "142 checked,
    #: 8 differ" instead of just showing 8 rows and leaving the rest ambiguous.
    matched: int = 0
    #: Same sku_code counted twice in one file.
    duplicates: list[str] = field(default_factory=list)

    @property
    def total_counted_qty(self) -> int:
        return sum(r.counted_qty for r in self.counted)

    @property
    def has_differences(self) -> bool:
        return bool(self.diffs)


def parse(data: bytes) -> Inspection:
    """Encoding, required columns and per-row issues. Never touches the DB."""
    return inspect(data, STOCKTAKE_CSV_SPEC)


def rows_from(data: bytes) -> tuple[list[CountedRow], list[str]]:
    """Valid rows, plus the sku_codes that appeared more than once.

    A duplicate is reported rather than summed. Two lines for one SKU usually
    means two people counted the same shelf, and adding them together doubles
    the stock — silently, and in the direction that hides a shortage. The FIRST
    count is kept so re-running the parse is deterministic; the screen shows
    both codes so the operator decides which is right.
    """
    seen: set[str] = set()
    out: list[CountedRow] = []
    duplicates: list[str] = []
    for row in iter_rows(data, STOCKTAKE_CSV_SPEC):
        sku = str(row[COL_SKU]).strip()
        if sku in seen:
            duplicates.append(sku)
            continue
        seen.add(sku)
        out.append(CountedRow(sku_code=sku, counted_qty=int(row[COL_COUNTED])))
    return out, duplicates


async def plan_from_rows(session: AsyncSession, rows: list[CountedRow]) -> StocktakePlan:
    """Compare the counted rows against current stock.

    Only the SKUs present in `rows` are looked at. Everything else in the
    catalogue is left exactly as it is — see the module docstring.
    """
    plan = StocktakePlan()
    if not rows:
        return plan

    codes = [r.sku_code for r in rows]
    found = await session.execute(
        select(MasterSku.sku_code, MasterSku.id).where(MasterSku.sku_code.in_(codes))
    )
    by_code: dict[str, int] = {code: mid for code, mid in found.all()}  # noqa: C416

    ids = list(by_code.values())
    snapshots = await session.execute(
        select(InventorySnapshot.master_sku_id, InventorySnapshot.on_hand_qty).where(
            InventorySnapshot.master_sku_id.in_(ids)
        )
    )
    on_hand: dict[int, int] = {mid: qty for mid, qty in snapshots.all()}  # noqa: C416

    for row in rows:
        master_id = by_code.get(row.sku_code)
        if master_id is None:
            plan.unknown.append(row)
            continue
        plan.counted.append(row)
        current = on_hand.get(master_id, 0)
        if current == row.counted_qty:
            plan.matched += 1
            continue
        plan.diffs.append(
            DiffInput(
                master_sku_id=master_id,
                current_qty=current,
                target_qty=row.counted_qty,
            )
        )
    return plan


async def build_plan(session: AsyncSession, data: bytes) -> tuple[Inspection, StocktakePlan]:
    inspection = parse(data)
    if inspection.fatal:
        return inspection, StocktakePlan()
    rows, duplicates = rows_from(data)
    plan = await plan_from_rows(session, rows)
    plan.duplicates = duplicates
    return inspection, plan


def scope_note(categories: list[str], counted: int) -> str:
    """What this instalment covered, in the operator's words.

    Recorded explicitly because it cannot be recovered afterwards: a run holds
    only the SKUs that DIFFERED, so a category counted and found entirely
    correct leaves no trace at all. Without this, "did we ever count the
    pierces?" has no answer.
    """
    where = "・".join(categories) if categories else "対象範囲の指定なし"
    return f"{where} / {counted}SKUを計数"


@dataclass(frozen=True, slots=True)
class SheetRow:
    """One line of a blank count sheet."""

    sku_code: str
    name: str
    category: str | None
    on_hand_qty: int


async def sheet_rows(
    session: AsyncSession, *, category_codes: list[str] | None = None
) -> list[SheetRow]:
    """The SKUs to print on a count sheet, optionally narrowed to categories.

    Uses `operational_conditions()`: archived masters and 在庫管理対象外 items
    are not on the shelf to be counted. Set parents ARE included — they hold no
    stock of their own, so they will read zero and an operator can confirm that
    rather than wonder why the sheet skipped them.
    """
    stmt = (
        select(
            MasterSku.sku_code,
            MasterSku.name,
            ProductCategory.name,
            InventorySnapshot.on_hand_qty,
        )
        .outerjoin(ProductCategory, ProductCategory.id == MasterSku.category_id)
        .outerjoin(InventorySnapshot, InventorySnapshot.master_sku_id == MasterSku.id)
        .where(*operational_conditions())
        .order_by(ProductCategory.name, MasterSku.sku_code)
    )
    if category_codes:
        stmt = stmt.where(ProductCategory.code.in_(category_codes))

    rows = await session.execute(stmt)
    return [
        SheetRow(sku_code=code, name=name, category=category, on_hand_qty=int(qty or 0))
        for code, name, category, qty in rows.all()
    ]


__all__ = [
    "COL_COUNTED",
    "COL_SKU",
    "STOCKTAKE_CSV_SPEC",
    "CountedRow",
    "SheetRow",
    "StocktakePlan",
    "build_plan",
    "parse",
    "plan_from_rows",
    "rows_from",
    "scope_note",
    "sheet_rows",
]
