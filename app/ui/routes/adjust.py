"""Manual inventory adjustment form."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.db import get_session
from app.models import InventorySnapshot, MasterSku
from app.services import InventoryInsufficientError, InventoryService, MasterSkuNotFoundError
from app.ui.auth import OperatorDep
from app.ui.deps import templates

router = APIRouter()


@router.get("/adjust")
async def adjust_form(
    request: Request,
    operator: OperatorDep,
    session: AsyncSession = Depends(get_session),
    master_sku_id: int | None = None,
) -> Response:
    stmt = (
        select(
            MasterSku.id,
            MasterSku.sku_code,
            MasterSku.name,
            func.coalesce(InventorySnapshot.on_hand_qty, 0).label("on_hand_qty"),
        )
        .outerjoin(InventorySnapshot, InventorySnapshot.master_sku_id == MasterSku.id)
        .order_by(MasterSku.sku_code)
    )
    rows = (await session.execute(stmt)).mappings().all()
    flash = request.query_params.get("flash")
    return templates.TemplateResponse(
        request,
        "adjust.html",
        {
            "operator": operator,
            "version": __version__,
            "master_skus": rows,
            "preselect": master_sku_id,
            "flash": _flash(flash),
        },
    )


@router.post("/adjust")
async def adjust_submit(
    operator: OperatorDep,
    master_sku_id: Annotated[int, Form()],
    quantity_delta: Annotated[int, Form()],
    reason: Annotated[str, Form()],
    session: AsyncSession = Depends(get_session),
) -> Response:
    async with session.begin():
        try:
            await InventoryService(session).manual_adjust(
                master_sku_id=master_sku_id,
                quantity_delta=quantity_delta,
                reason=reason.strip(),
                operator=operator,
            )
        except InventoryInsufficientError:
            await session.rollback()
            return RedirectResponse(url="/admin/adjust?flash=insufficient", status_code=303)
        except MasterSkuNotFoundError:
            await session.rollback()
            return RedirectResponse(url="/admin/adjust?flash=notfound", status_code=303)
        except ValueError:
            await session.rollback()
            return RedirectResponse(url="/admin/adjust?flash=invalid", status_code=303)
    return RedirectResponse(url="/admin/adjust?flash=applied", status_code=303)


def _flash(token: str | None) -> dict[str, str] | None:
    table = {
        "applied": ("ok", "在庫を調整しました。"),
        "insufficient": ("error", "調整後の在庫がマイナスになるため拒否しました。"),
        "notfound": ("error", "指定したマスターSKUが見つかりません。"),
        "invalid": ("error", "調整数は 0 以外の整数を指定してください。"),
    }
    if not token or token not in table:
        return None
    kind, message = table[token]
    return {"kind": kind, "message": message}
