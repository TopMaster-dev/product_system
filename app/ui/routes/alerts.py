"""Mapping alerts viewer + one-click resolution."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.db import get_session
from app.models import MappingAlert, MasterSku
from app.services import MappingService
from app.ui.auth import OperatorDep
from app.ui.deps import templates

router = APIRouter()


@router.get("/alerts")
async def alerts_list(
    request: Request,
    operator: OperatorDep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    alerts = (
        (
            await session.execute(
                select(MappingAlert).order_by(
                    MappingAlert.status, MappingAlert.first_seen_at.desc()
                )
            )
        )
        .scalars()
        .all()
    )
    master_skus = (
        (await session.execute(select(MasterSku).order_by(MasterSku.sku_code))).scalars().all()
    )
    return templates.TemplateResponse(
        request,
        "alerts.html",
        {
            "operator": operator,
            "version": __version__,
            "alerts": alerts,
            "master_skus": master_skus,
            "flash": _flash(request.query_params.get("flash")),
        },
    )


@router.post("/alerts/{alert_id}/resolve")
async def alerts_resolve(
    alert_id: int,
    operator: OperatorDep,
    master_sku_id: Annotated[int, Form()],
    session: AsyncSession = Depends(get_session),
) -> Response:
    async with session.begin():
        alert = await session.get(MappingAlert, alert_id)
        if alert is None:
            return RedirectResponse(url="/admin/alerts?flash=notfound", status_code=303)
        replayed = await MappingService(session).resolve_alert(
            channel=alert.channel,
            channel_sku=alert.channel_sku,
            marketplace_id=alert.marketplace_id,
            master_sku_id=master_sku_id,
        )
    return RedirectResponse(url=f"/admin/alerts?flash=resolved:{replayed}", status_code=303)


def _flash(token: str | None) -> dict[str, str] | None:
    if not token:
        return None
    parts = token.split(":")
    if parts[0] == "resolved" and len(parts) == 2:
        return {
            "kind": "ok",
            "message": f"解決しました。保留中の注文 {parts[1]} 件を再処理しました。",
        }
    if parts[0] == "notfound":
        return {"kind": "error", "message": "アラートが見つかりません。"}
    return None
