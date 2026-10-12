"""Category taxonomy — the read model and the rules that protect it.

The screen shows 大分類 with their 中分類 nested underneath, each with the number
of SKUs assigned to it. Two things here are less obvious than they look.

**Assignment counts roll up.** A SKU assigned to a 中分類 counts toward its
parent too, otherwise a 大分類 with only sub-categories reads "0 SKUs" and looks
broken. The parent's own count (SKUs assigned directly to the 大分類) is kept
separately so the two can be told apart.

**Counting uses `operational_conditions`**, so archived and 在庫管理対象外 SKUs
are excluded — the same population the inventory list shows. Counting them here
would make "未分類 358件" appear right after the archive cleanup deliberately
retired ~355 masters, sending the client hunting for work that does not exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import ColumnElement, Select, case, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.csv_intake import ColumnSpec, CsvSpec, Inspection, OnEmpty, inspect, iter_rows
from app.models import MasterSku, ProductCategory
from app.services.sku_scope import operational_conditions

#: The client fills this in Excel, so the header comes back in Japanese as often
#: as not. `category_code` uses OnEmpty.KEEP: a blank cell is how a SKU gets
#: UN-assigned, so the row must survive rather than be skipped.
SKU_CATEGORY_CSV = CsvSpec(
    columns=(
        ColumnSpec(canonical="sku_code", aliases=("SKUコード", "SKU", "商品コード")),
        ColumnSpec(
            canonical="category_code",
            aliases=("カテゴリコード", "カテゴリ", "category"),
            on_empty=OnEmpty.KEEP,
        ),
    )
)


#: The 大分類 of a 中分類, for 「大分類 / 中分類」 labels in list queries.
_PARENT_CATEGORY = aliased(ProductCategory, name="parent_category")


def category_label_column() -> ColumnElement[Any]:
    """「大分類 / 中分類」 for a SKU in a 中分類, the name alone for a 大分類,
    NULL for none. Pair with `join_category` on a query selecting MasterSku."""
    return case(
        (
            _PARENT_CATEGORY.id.is_not(None),
            _PARENT_CATEGORY.name + " / " + ProductCategory.name,
        ),
        else_=ProductCategory.name,
    ).label("category_name")


def join_category(stmt: Select[Any]) -> Select[Any]:
    return stmt.outerjoin(ProductCategory, ProductCategory.id == MasterSku.category_id).outerjoin(
        _PARENT_CATEGORY, _PARENT_CATEGORY.id == ProductCategory.parent_id
    )


@dataclass(slots=True)
class CategoryNode:
    category: ProductCategory
    #: SKUs assigned directly to this category.
    own_sku_count: int = 0
    children: list[CategoryNode] = field(default_factory=list)

    @property
    def total_sku_count(self) -> int:
        """Including everything under it — a 大分類 whose SKUs all live in its
        children must not read as empty."""
        return self.own_sku_count + sum(c.total_sku_count for c in self.children)

    @property
    def has_active_children(self) -> bool:
        return any(c.category.is_active for c in self.children)


@dataclass(slots=True)
class CategoryOverview:
    roots: list[CategoryNode]
    unclassified_count: int
    #: Every category, active or not, for the assignment dropdowns.
    total_categories: int

    @property
    def classified_count(self) -> int:
        return sum(r.total_sku_count for r in self.roots)


async def sku_counts_by_category(session: AsyncSession) -> dict[int, int]:
    """category_id -> assigned SKU count, over the operational population."""
    rows = await session.execute(
        select(MasterSku.category_id, func.count())
        .where(MasterSku.category_id.is_not(None), *operational_conditions())
        .group_by(MasterSku.category_id)
    )
    return {cid: count for cid, count in rows.all() if cid is not None}


async def count_unclassified(session: AsyncSession) -> int:
    """SKUs with no category. Shown as its own bucket rather than hidden: if the
    category totals silently omitted them, they would not add up to the whole and
    nobody could tell which number was wrong."""
    return (
        await session.scalar(
            select(func.count())
            .select_from(MasterSku)
            .where(MasterSku.category_id.is_(None), *operational_conditions())
        )
        or 0
    )


async def load_overview(session: AsyncSession) -> CategoryOverview:
    """The whole taxonomy in two queries plus a count.

    The tree is built in Python rather than with a recursive CTE — it is at most
    two levels deep by CHECK constraint, and a few dozen rows.
    """
    categories = list(
        (
            await session.execute(
                select(ProductCategory).order_by(
                    ProductCategory.level,
                    ProductCategory.sort_order,
                    ProductCategory.code,
                )
            )
        )
        .scalars()
        .all()
    )
    counts = await sku_counts_by_category(session)

    nodes = {c.id: CategoryNode(category=c, own_sku_count=counts.get(c.id, 0)) for c in categories}
    roots: list[CategoryNode] = []
    for category in categories:
        node = nodes[category.id]
        parent = nodes.get(category.parent_id) if category.parent_id else None
        if parent is None:
            roots.append(node)
        else:
            parent.children.append(node)

    return CategoryOverview(
        roots=roots,
        unclassified_count=await count_unclassified(session),
        total_categories=len(categories),
    )


class CategoryError(ValueError):
    """A rule the taxonomy will not bend on, phrased for an operator."""


async def assert_can_deactivate(session: AsyncSession, category: ProductCategory) -> None:
    """A 大分類 cannot be disabled while it still has active 中分類.

    Allowing it would orphan the children on screen: they would stay active and
    assignable while their parent is gone from every grouped view, so SKUs could
    be filed into a branch nobody can see.
    """
    active_children = (
        await session.scalar(
            select(func.count())
            .select_from(ProductCategory)
            .where(
                ProductCategory.parent_id == category.id,
                ProductCategory.is_active.is_(True),
            )
        )
        or 0
    )
    if active_children:
        raise CategoryError(
            f"有効な中分類が {active_children} 件あるため無効化できません。"
            "先に中分類を無効化してください。"
        )


async def assert_can_delete(session: AsyncSession, category: ProductCategory) -> None:
    """Deletion is refused whenever anything still points at the category.

    The database enforces this with ON DELETE RESTRICT; checking first turns an
    IntegrityError into a sentence an operator can act on.
    """
    assigned = (
        await session.scalar(
            select(func.count()).select_from(MasterSku).where(MasterSku.category_id == category.id)
        )
        or 0
    )
    if assigned:
        raise CategoryError(
            f"{assigned} 件のSKUが割り当てられているため削除できません。"
            "無効化するか、先にSKUの分類を変更してください。"
        )
    children = (
        await session.scalar(
            select(func.count())
            .select_from(ProductCategory)
            .where(ProductCategory.parent_id == category.id)
        )
        or 0
    )
    if children:
        raise CategoryError(
            f"中分類が {children} 件あるため削除できません。先に中分類を削除してください。"
        )


@dataclass(frozen=True, slots=True)
class AssignmentChange:
    """One SKU whose category the import would change, as the preview shows it."""

    sku_code: str
    name: str
    before: str
    after: str


#: How the preview names "no category" on each side of a change.
UNCLASSIFIED_LABEL = "未分類"
CLEARED_LABEL = "未分類 (解除)"


class AssignmentPlan:
    """What an import would do, resolved but not yet written."""

    def __init__(self) -> None:
        #: category_id (or None to clear) -> master_sku ids
        self.by_category: dict[int | None, list[int]] = {}
        self.unknown_skus: list[str] = []
        self.unknown_categories: list[str] = []
        self.unchanged = 0
        #: Per SKU, before -> after. The preview showed only counts, so the client
        #: could not see WHICH product would move where (acceptance Q&A,
        #: 2026-10-12).
        self.changes: list[AssignmentChange] = []
        #: SKU codes that appear on more than one valid row; the last row wins.
        self.duplicate_skus: list[str] = []

    @property
    def assigned(self) -> int:
        return sum(len(v) for k, v in self.by_category.items() if k is not None)

    @property
    def cleared(self) -> int:
        return len(self.by_category.get(None, []))


async def category_labels(session: AsyncSession, ids: set[int]) -> dict[int, str]:
    """「大分類 / 中分類」 for a 中分類, the name alone for a 大分類."""
    if not ids:
        return {}
    rows = (
        await session.execute(
            select(ProductCategory.id, ProductCategory.name, ProductCategory.parent_id).where(
                ProductCategory.id.in_(ids)
            )
        )
    ).all()
    parents = {pid for _, _, pid in rows if pid is not None}
    parent_names: dict[int, str] = {}
    if parents:
        parent_names = dict(
            (
                await session.execute(
                    select(ProductCategory.id, ProductCategory.name).where(
                        ProductCategory.id.in_(parents)
                    )
                )
            ).all()  # type: ignore[arg-type]
        )
    return {
        cid: f"{parent_names[pid]} / {name}" if pid in parent_names else name
        for cid, name, pid in rows
    }


async def plan_assignments(session: AsyncSession, data: bytes) -> tuple[Inspection, AssignmentPlan]:
    """Resolve a CSV against the database without writing anything.

    Runs for both the preview and the apply, so the two can never disagree about
    what the file means.

    Resolved PER SKU, last valid row winning. The plan used to queue each row
    under its target category, so a SKU listed twice was queued twice and the
    one applied last depended on which category happened to be met first —
    not on the order of the rows the client wrote.
    """
    inspection = inspect(data, SKU_CATEGORY_CSV)
    plan = AssignmentPlan()
    if inspection.fatal:
        return inspection, plan

    rows = list(iter_rows(data, SKU_CATEGORY_CSV))
    wanted_skus = {r["sku_code"] for r in rows}
    wanted_codes = {r["category_code"] for r in rows if r.get("category_code")}

    sku_by_code = {
        code: (mid, name, current)
        for code, mid, name, current in (
            await session.execute(
                select(
                    MasterSku.sku_code, MasterSku.id, MasterSku.name, MasterSku.category_id
                ).where(MasterSku.sku_code.in_(wanted_skus or [""]))
            )
        ).all()
    }
    category_rows = await session.execute(
        select(ProductCategory.code, ProductCategory.id).where(
            ProductCategory.code.in_(wanted_codes or [""])
        )
    )
    category_by_code: dict[str, int] = dict(category_rows.all())  # type: ignore[arg-type]

    #: sku_code -> target category (None = clear), in first-seen order.
    targets: dict[str, int | None] = {}
    for row in rows:
        sku_code = row["sku_code"]
        if sku_code not in sku_by_code:
            plan.unknown_skus.append(sku_code)
            continue

        raw_code = row.get("category_code") or ""
        if not raw_code:
            # Blank means "remove the category" — a real correction, not a typo.
            target: int | None = None
        else:
            target = category_by_code.get(raw_code)
            if target is None:
                plan.unknown_categories.append(raw_code)
                continue

        if sku_code in targets and sku_code not in plan.duplicate_skus:
            plan.duplicate_skus.append(sku_code)
        targets[sku_code] = target

    current_ids = {cur for _, _, cur in sku_by_code.values() if cur is not None}
    target_ids = {t for t in targets.values() if t is not None}
    labels = await category_labels(session, current_ids | target_ids)

    for sku_code, target in targets.items():
        master_id, name, current = sku_by_code[sku_code]
        if target == current:
            # Re-importing the same file is a no-op, which is what makes a
            # corrected re-send safe to run twice.
            plan.unchanged += 1
            continue
        plan.by_category.setdefault(target, []).append(master_id)
        plan.changes.append(
            AssignmentChange(
                sku_code=sku_code,
                name=name,
                before=labels.get(current, UNCLASSIFIED_LABEL) if current else UNCLASSIFIED_LABEL,
                after=labels.get(target, "") if target else CLEARED_LABEL,
            )
        )

    return inspection, plan
