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

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import MasterSku, ProductCategory
from app.services.sku_scope import operational_conditions
from app.ui.csv_intake import ColumnSpec, CsvSpec, Inspection, OnEmpty, inspect, iter_rows

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


class AssignmentPlan:
    """What an import would do, resolved but not yet written."""

    def __init__(self) -> None:
        #: category_id (or None to clear) -> master_sku ids
        self.by_category: dict[int | None, list[int]] = {}
        self.unknown_skus: list[str] = []
        self.unknown_categories: list[str] = []
        self.unchanged = 0

    @property
    def assigned(self) -> int:
        return sum(len(v) for k, v in self.by_category.items() if k is not None)

    @property
    def cleared(self) -> int:
        return len(self.by_category.get(None, []))


async def plan_assignments(session: AsyncSession, data: bytes) -> tuple[Inspection, AssignmentPlan]:
    """Resolve a CSV against the database without writing anything.

    Runs for both the preview and the apply, so the two can never disagree about
    what the file means.
    """
    inspection = inspect(data, SKU_CATEGORY_CSV)
    plan = AssignmentPlan()
    if inspection.fatal:
        return inspection, plan

    rows = list(iter_rows(data, SKU_CATEGORY_CSV))
    wanted_skus = {r["sku_code"] for r in rows}
    wanted_codes = {r["category_code"] for r in rows if r.get("category_code")}

    sku_by_code = {
        code: (mid, current)
        for code, mid, current in (
            await session.execute(
                select(MasterSku.sku_code, MasterSku.id, MasterSku.category_id).where(
                    MasterSku.sku_code.in_(wanted_skus or [""])
                )
            )
        ).all()
    }
    category_rows = await session.execute(
        select(ProductCategory.code, ProductCategory.id).where(
            ProductCategory.code.in_(wanted_codes or [""])
        )
    )
    category_by_code: dict[str, int] = dict(category_rows.all())  # type: ignore[arg-type]

    for row in rows:
        sku_code = row["sku_code"]
        found = sku_by_code.get(sku_code)
        if found is None:
            plan.unknown_skus.append(sku_code)
            continue
        master_id, current_category = found

        raw_code = row.get("category_code") or ""
        if not raw_code:
            # Blank means "remove the category" — a real correction, not a typo.
            target: int | None = None
        else:
            target = category_by_code.get(raw_code)
            if target is None:
                plan.unknown_categories.append(raw_code)
                continue

        if target == current_category:
            # Re-importing the same file is a no-op, which is what makes a
            # corrected re-send safe to run twice.
            plan.unchanged += 1
            continue
        plan.by_category.setdefault(target, []).append(master_id)

    return inspection, plan
