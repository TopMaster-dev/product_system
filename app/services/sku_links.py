"""Finding a SKU's page, and following a set to its components and back.

Since 2026-10-03 the stock screens do not list set / shared-stock parents: a
parent holds no stock of its own, and listed it read 「0・ゼロ」. That left no
way to reach a parent's own page — searching 「N108」 in 在庫一覧 found only the
shared component, and the client asked how to open the 45cm and 42cm pages
individually (acceptance Q&A, 2026-10-12). These are the lookups the screens use
to put the way back in: search that includes parents, and links in both
directions between a set and what it is built from.

Read-only, and every query is a single round trip whatever the page size.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import BundleComponent, InventorySnapshot, MasterSku
from app.services.sku_scope import bundle_parent_conditions

#: Rows a search returns. A search is for finding a page, not browsing.
SEARCH_LIMIT = 30


@dataclass(frozen=True, slots=True)
class SkuRef:
    id: int
    sku_code: str
    name: str


@dataclass(frozen=True, slots=True)
class SkuHit:
    id: int
    sku_code: str
    name: str
    is_bundle: bool
    archived: bool


@dataclass(frozen=True, slots=True)
class ComponentRef:
    id: int
    sku_code: str
    name: str
    quantity_per: int
    on_hand_qty: int


def _search_order(q: str):  # type: ignore[no-untyped-def]
    """Exact code first, then codes starting with the term, then the rest."""
    return case(
        (func.lower(MasterSku.sku_code) == q.lower(), 0),
        (MasterSku.sku_code.ilike(f"{q}%"), 1),
        else_=2,
    )


async def search_skus(session: AsyncSession, q: str, *, limit: int = SEARCH_LIMIT) -> list[SkuHit]:
    """Every master whose code or name contains `q`, parents and retired ones
    included — the point is to reach a page, whatever kind of SKU it is."""
    q = q.strip()
    if not q:
        return []
    like = f"%{q}%"
    rows = await session.execute(
        select(
            MasterSku.id,
            MasterSku.sku_code,
            MasterSku.name,
            MasterSku.is_bundle,
            MasterSku.archived_at,
        )
        .where(or_(MasterSku.sku_code.ilike(like), MasterSku.name.ilike(like)))
        .order_by(_search_order(q), MasterSku.sku_code)
        .limit(limit)
    )
    return [
        SkuHit(
            id=i, sku_code=code, name=name, is_bundle=bool(bundle), archived=archived is not None
        )
        for i, code, name, bundle, archived in rows.all()
    ]


async def search_bundle_parents(
    session: AsyncSession, q: str, *, limit: int = SEARCH_LIMIT
) -> list[SkuRef]:
    """Parents matching `q`, for the stock screens that no longer list them."""
    q = q.strip()
    if not q:
        return []
    like = f"%{q}%"
    rows = await session.execute(
        select(MasterSku.id, MasterSku.sku_code, MasterSku.name)
        .where(
            *bundle_parent_conditions(),
            or_(MasterSku.sku_code.ilike(like), MasterSku.name.ilike(like)),
        )
        .order_by(_search_order(q), MasterSku.sku_code)
        .limit(limit)
    )
    return [SkuRef(id=i, sku_code=code, name=name) for i, code, name in rows.all()]


async def parents_of(session: AsyncSession, component_ids: list[int]) -> dict[int, list[SkuRef]]:
    """component id -> the sets that draw on it (the 45cm and 42cm of a chain)."""
    if not component_ids:
        return {}
    rows = await session.execute(
        select(
            BundleComponent.component_master_sku_id,
            MasterSku.id,
            MasterSku.sku_code,
            MasterSku.name,
        )
        .join(MasterSku, MasterSku.id == BundleComponent.bundle_master_sku_id)
        .where(
            BundleComponent.component_master_sku_id.in_(component_ids),
            *bundle_parent_conditions(),
        )
        .order_by(MasterSku.sku_code)
    )
    out: dict[int, list[SkuRef]] = {}
    for component_id, pid, code, name in rows.all():
        out.setdefault(component_id, []).append(SkuRef(id=pid, sku_code=code, name=name))
    return out


async def components_of(
    session: AsyncSession, parent_ids: list[int]
) -> dict[int, list[ComponentRef]]:
    """parent id -> what it is built from, with each component's stock."""
    if not parent_ids:
        return {}
    rows = await session.execute(
        select(
            BundleComponent.bundle_master_sku_id,
            MasterSku.id,
            MasterSku.sku_code,
            MasterSku.name,
            BundleComponent.quantity_per,
            func.coalesce(InventorySnapshot.on_hand_qty, 0),
        )
        .join(MasterSku, MasterSku.id == BundleComponent.component_master_sku_id)
        .outerjoin(InventorySnapshot, InventorySnapshot.master_sku_id == MasterSku.id)
        .where(BundleComponent.bundle_master_sku_id.in_(parent_ids))
        .order_by(MasterSku.sku_code)
    )
    out: dict[int, list[ComponentRef]] = {}
    for parent_id, cid, code, name, per, qty in rows.all():
        out.setdefault(parent_id, []).append(
            ComponentRef(
                id=cid, sku_code=code, name=name, quantity_per=int(per), on_hand_qty=int(qty)
            )
        )
    return out
