"""Manual inventory adjustment — reason templates, before/after preview,
a confirm step, and recent-history visibility."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import RowMapping, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.db import get_session
from app.models import InventoryEvent, InventoryEventTypeEnum, InventorySnapshot, MasterSku
from app.services import InventoryInsufficientError, InventoryService, MasterSkuNotFoundError
from app.ui.auth import OperatorDep
from app.ui.deps import templates

router = APIRouter()

# Preset adjustment reasons (P1B-043) — chosen from the client's operational
# vocabulary. Selecting one fills the free-text reason field; free text is still
# allowed for anything not covered here.
REASON_TEMPLATES = ["不良品", "破損", "検品NG", "紛失", "棚卸差異", "POPUP戻り在庫"]


async def _sku_rows(session: AsyncSession) -> Sequence[RowMapping]:
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
    return (await session.execute(stmt)).mappings().all()


@router.get("/adjust")
async def adjust_form(
    request: Request,
    operator: OperatorDep,
    session: AsyncSession = Depends(get_session),
    master_sku_id: int | None = None,
) -> Response:
    rows = await _sku_rows(session)
    recent = (
        (
            await session.execute(
                select(
                    InventoryEvent.quantity_delta,
                    InventoryEvent.reason,
                    InventoryEvent.operator,
                    InventoryEvent.occurred_at,
                    MasterSku.sku_code,
                )
                .join(MasterSku, MasterSku.id == InventoryEvent.master_sku_id)
                .where(InventoryEvent.event_type == InventoryEventTypeEnum.MANUAL_ADJUST)
                .order_by(InventoryEvent.occurred_at.desc())
                .limit(10)
            )
        )
        .mappings()
        .all()
    )
    return templates.TemplateResponse(
        request,
        "adjust.html",
        {
            "operator": operator,
            "version": __version__,
            "master_skus": rows,
            "preselect": master_sku_id,
            "reason_templates": REASON_TEMPLATES,
            "recent": recent,
            "flash": _flash(request.query_params.get("flash")),
        },
    )


@router.post("/adjust/confirm")
async def adjust_confirm(
    request: Request,
    operator: OperatorDep,
    master_sku_id: Annotated[int, Form()],
    quantity_delta: Annotated[int, Form()],
    reason: Annotated[str, Form()],
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Validate the inputs and render a confirmation screen (before/after) so the
    operator explicitly confirms before the adjustment is applied (P1B-046)."""
    row = (
        await session.execute(
            select(
                MasterSku.sku_code,
                MasterSku.name,
                func.coalesce(InventorySnapshot.on_hand_qty, 0).label("on_hand_qty"),
            )
            .outerjoin(InventorySnapshot, InventorySnapshot.master_sku_id == MasterSku.id)
            .where(MasterSku.id == master_sku_id)
        )
    ).first()
    if row is None:
        return RedirectResponse(url="/admin/adjust?flash=notfound", status_code=303)
    if quantity_delta == 0:
        return RedirectResponse(url="/admin/adjust?flash=invalid", status_code=303)
    current = row.on_hand_qty
    after = current + quantity_delta
    if after < 0:
        return RedirectResponse(url="/admin/adjust?flash=insufficient", status_code=303)

    return templates.TemplateResponse(
        request,
        "adjust_confirm.html",
        {
            "operator": operator,
            "version": __version__,
            "master_sku_id": master_sku_id,
            "sku_code": row.sku_code,
            "name": row.name,
            "current": current,
            "after": after,
            "quantity_delta": quantity_delta,
            "reason": reason.strip(),
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
        "applied": ("ok", "在庫を調整しました。下の履歴に反映されています。"),
        "insufficient": ("error", "調整後の在庫がマイナスになるため拒否しました。"),
        "notfound": ("error", "指定したマスターSKUが見つかりません。"),
        "invalid": ("error", "調整数は 0 以外の整数を指定してください。"),
    }
    if not token or token not in table:
        return None
    kind, message = table[token]
    return {"kind": kind, "message": message}
