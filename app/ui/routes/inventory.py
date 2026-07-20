"""Inventory list view — search, status buckets, sorting, count badges,
best-seller flagging, and CSV export."""

from __future__ import annotations

import csv
import io
import math
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import Select, case, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.config import get_settings
from app.db import get_session
from app.models import InventoryEvent, InventoryEventTypeEnum, InventorySnapshot, MasterSku
from app.ui.auth import OperatorDep
from app.ui.deps import templates

router = APIRouter()

PAGE_SIZE = 50


async def best_seller_ids(session: AsyncSession, *, window_days: int, top_percent: int) -> set[int]:
    """master_sku_ids in the top `top_percent`% by consumed quantity
    (order_consumed events) over the trailing `window_days`. Basis for the
    売れ筋 flag; Phase 2 will layer richer velocity analytics on top."""
    since = datetime.now(UTC) - timedelta(days=window_days)
    consumed = (
        select(
            InventoryEvent.master_sku_id.label("mid"),
            func.sum(func.abs(InventoryEvent.quantity_delta)).label("qty"),
        )
        .where(
            InventoryEvent.event_type == InventoryEventTypeEnum.ORDER_CONSUMED,
            InventoryEvent.occurred_at >= since,
        )
        .group_by(InventoryEvent.master_sku_id)
        .subquery()
    )
    total = await session.scalar(select(func.count()).select_from(consumed)) or 0
    if total == 0:
        return set()
    limit = max(1, math.ceil(total * top_percent / 100))
    rows = await session.execute(
        select(consumed.c.mid).order_by(consumed.c.qty.desc()).limit(limit)
    )
    return {mid for (mid,) in rows.all()}


# on_hand quantity as a coalesced expression (missing snapshot => 0).
_QTY = func.coalesce(InventorySnapshot.on_hand_qty, 0)

# Status buckets (mutually exclusive): negative < 0, zero == 0,
# low 1..9, normal >= 10. Ranked so problem SKUs sort first by default.
_STATUS_RANK = case(
    (_QTY < 0, 0),
    (_QTY == 0, 1),
    (_QTY < 10, 2),
    else_=3,
)

# Sort key -> ORDER BY column.
_SORT_COLUMNS = {
    "status": _STATUS_RANK,
    "sku": MasterSku.sku_code,
    "name": MasterSku.name,
    "qty": _QTY,
    "updated": InventorySnapshot.updated_at,
}


def _base_query(q: str) -> Select[Any]:
    stmt = (
        select(
            MasterSku.id,
            MasterSku.sku_code,
            MasterSku.name,
            MasterSku.jan_code,
            _QTY.label("on_hand_qty"),
            InventorySnapshot.updated_at,
        )
        .select_from(MasterSku)
        .outerjoin(InventorySnapshot, InventorySnapshot.master_sku_id == MasterSku.id)
    )
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(MasterSku.sku_code.ilike(like), MasterSku.name.ilike(like)))
    return stmt


def _apply_filter(stmt: Select[Any], filter_mode: str) -> Select[Any]:
    if filter_mode == "negative":
        return stmt.where(_QTY < 0)
    if filter_mode == "zero":
        return stmt.where(_QTY == 0)
    if filter_mode == "low":
        return stmt.where(_QTY >= 1, _QTY < 10)
    if filter_mode == "normal":
        return stmt.where(_QTY >= 10)
    return stmt


def _apply_sort(stmt: Select[Any], sort: str, direction: str) -> Select[Any]:
    col = _SORT_COLUMNS.get(sort, _STATUS_RANK)
    ordered = col.desc() if direction == "desc" else col.asc()
    # Stable tiebreaker so equal-rank rows keep a deterministic order.
    return stmt.order_by(ordered, MasterSku.sku_code)


@router.get("/inventory")
async def inventory_list(
    request: Request,
    operator: OperatorDep,
    session: AsyncSession = Depends(get_session),
    q: str = "",
    filter: str = "all",
    sort: str = "status",
    dir: str = "asc",
    offset: int = 0,
) -> Response:
    if sort not in _SORT_COLUMNS:
        sort = "status"
    if dir not in ("asc", "desc"):
        dir = "asc"

    settings = get_settings()
    best_sellers = await best_seller_ids(
        session,
        window_days=settings.best_seller_window_days,
        top_percent=settings.best_seller_top_percent,
    )

    stmt = _base_query(q)
    if filter == "bestseller":
        stmt = stmt.where(MasterSku.id.in_(best_sellers or {-1}))
    else:
        stmt = _apply_filter(stmt, filter)
    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    # Status counts over the q-filtered set (independent of the state filter) so
    # the badges always show the full breakdown for the current search.
    counts_stmt = (
        select(
            func.coalesce(func.sum(case((_QTY < 0, 1), else_=0)), 0),
            func.coalesce(func.sum(case((_QTY == 0, 1), else_=0)), 0),
            func.coalesce(func.sum(case((_QTY.between(1, 9), 1), else_=0)), 0),
        )
        .select_from(MasterSku)
        .outerjoin(InventorySnapshot, InventorySnapshot.master_sku_id == MasterSku.id)
    )
    if q:
        like = f"%{q}%"
        counts_stmt = counts_stmt.where(
            or_(MasterSku.sku_code.ilike(like), MasterSku.name.ilike(like))
        )
    counts_row = (await session.execute(counts_stmt)).one()
    counts = {"negative": counts_row[0], "zero": counts_row[1], "low": counts_row[2]}

    stmt = _apply_sort(stmt, sort, dir).offset(offset).limit(PAGE_SIZE)
    rows = (await session.execute(stmt)).mappings().all()

    base = {"q": q, "filter": filter, "sort": sort, "dir": dir}
    pagination = {
        "total": total,
        "offset": offset,
        "has_prev": offset > 0,
        "has_next": offset + PAGE_SIZE < total,
        "qs_prev": urlencode({**base, "offset": max(0, offset - PAGE_SIZE)}),
        "qs_next": urlencode({**base, "offset": offset + PAGE_SIZE}),
    }

    return templates.TemplateResponse(
        request,
        "inventory_list.html",
        {
            "operator": operator,
            "version": __version__,
            "rows": rows,
            "q": q,
            "filter_mode": filter,
            "sort": sort,
            "dir": dir,
            "counts": counts,
            "best_sellers": best_sellers,
            "export_qs": urlencode({"q": q, "filter": filter, "sort": sort, "dir": dir}),
            "pagination": pagination,
        },
    )


@router.get("/inventory/export.csv")
async def inventory_export(
    operator: OperatorDep,
    session: AsyncSession = Depends(get_session),
    q: str = "",
    filter: str = "all",
    sort: str = "status",
    dir: str = "asc",
) -> Response:
    if sort not in _SORT_COLUMNS:
        sort = "status"
    if dir not in ("asc", "desc"):
        dir = "asc"

    settings = get_settings()
    best_sellers = await best_seller_ids(
        session,
        window_days=settings.best_seller_window_days,
        top_percent=settings.best_seller_top_percent,
    )
    stmt = _base_query(q)
    if filter == "bestseller":
        stmt = stmt.where(MasterSku.id.in_(best_sellers or {-1}))
    else:
        stmt = _apply_filter(stmt, filter)
    stmt = _apply_sort(stmt, sort, dir)
    rows = (await session.execute(stmt)).mappings().all()

    def _state(qty: int) -> str:
        if qty < 0:
            return "マイナス"
        if qty == 0:
            return "ゼロ"
        if qty < 10:
            return "低在庫"
        return "正常"

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["sku_code", "name", "jan_code", "on_hand_qty", "status", "best_seller", "updated_at"]
    )
    for r in rows:
        writer.writerow(
            [
                r["sku_code"],
                r["name"],
                r["jan_code"] or "",
                r["on_hand_qty"],
                _state(r["on_hand_qty"]),
                "はい" if r["id"] in best_sellers else "",
                r["updated_at"].strftime("%Y-%m-%d %H:%M:%S") if r["updated_at"] else "",
            ]
        )
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="inventory.csv"'},
    )
