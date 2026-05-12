"""Inventory list view."""

from __future__ import annotations

from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.db import get_session
from app.models import InventorySnapshot, MasterSku
from app.ui.auth import OperatorDep
from app.ui.deps import templates

router = APIRouter()

PAGE_SIZE = 50


@router.get("/inventory")
async def inventory_list(
    request: Request,
    operator: OperatorDep,
    session: AsyncSession = Depends(get_session),
    q: str = "",
    filter: str = "all",
    offset: int = 0,
) -> Response:
    stmt = (
        select(
            MasterSku.id,
            MasterSku.sku_code,
            MasterSku.name,
            MasterSku.jan_code,
            func.coalesce(InventorySnapshot.on_hand_qty, 0).label("on_hand_qty"),
            InventorySnapshot.updated_at,
        )
        .select_from(MasterSku)
        .outerjoin(InventorySnapshot, InventorySnapshot.master_sku_id == MasterSku.id)
    )

    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(MasterSku.sku_code.ilike(like), MasterSku.name.ilike(like)))

    if filter == "low":
        stmt = stmt.where(func.coalesce(InventorySnapshot.on_hand_qty, 0) < 10)
    elif filter == "negative":
        stmt = stmt.where(func.coalesce(InventorySnapshot.on_hand_qty, 0) < 0)

    count_stmt = select(func.count()).select_from(stmt.subquery())
    total = await session.scalar(count_stmt) or 0

    stmt = stmt.order_by(MasterSku.sku_code).offset(offset).limit(PAGE_SIZE)
    rows = (await session.execute(stmt)).mappings().all()

    base = {"q": q, "filter": filter}
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
            "pagination": pagination,
        },
    )
