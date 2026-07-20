"""Mapping alerts — 3-state workflow (未対応 / 対応中 / 解決済み), count badges,
channel/SKU filter, a status summary, and inline resolution."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.db import get_session
from app.models import ChannelEnum, MappingAlert, MappingAlertStatusEnum, MasterSku
from app.services import MappingService
from app.ui.auth import OperatorDep
from app.ui.deps import templates

router = APIRouter()

_TABS = (
    MappingAlertStatusEnum.OPEN.value,
    MappingAlertStatusEnum.IN_PROGRESS.value,
    MappingAlertStatusEnum.RESOLVED.value,
)
_OUTSTANDING = [MappingAlertStatusEnum.OPEN.value, MappingAlertStatusEnum.IN_PROGRESS.value]


@router.get("/alerts")
async def alerts_list(
    request: Request,
    operator: OperatorDep,
    session: AsyncSession = Depends(get_session),
    status: str = MappingAlertStatusEnum.OPEN.value,
    channel: str = "",
    q: str = "",
) -> Response:
    if status not in _TABS:
        status = MappingAlertStatusEnum.OPEN.value

    # Per-state counts for the tab badges.
    counts = dict.fromkeys(_TABS, 0)
    for st, n in (
        await session.execute(
            select(MappingAlert.status, func.count()).group_by(MappingAlert.status)
        )
    ).all():
        if st in counts:
            counts[st] = n

    # Summary: outstanding by channel + new in the last 7 days.
    by_channel = [
        (ch, n)
        for ch, n in (
            await session.execute(
                select(MappingAlert.channel, func.count())
                .where(MappingAlert.status.in_(_OUTSTANDING))
                .group_by(MappingAlert.channel)
                .order_by(func.count().desc())
            )
        ).all()
    ]
    week_ago = datetime.now(UTC) - timedelta(days=7)
    recent_new = (
        await session.scalar(
            select(func.count())
            .select_from(MappingAlert)
            .where(
                MappingAlert.status.in_(_OUTSTANDING),
                MappingAlert.first_seen_at >= week_ago,
            )
        )
        or 0
    )

    # The alert list for the active tab, filtered by channel / SKU.
    stmt = select(MappingAlert).where(MappingAlert.status == status)
    if channel:
        stmt = stmt.where(MappingAlert.channel == channel)
    if q:
        stmt = stmt.where(MappingAlert.channel_sku.ilike(f"%{q}%"))
    stmt = stmt.order_by(MappingAlert.first_seen_at.desc())
    alerts = (await session.execute(stmt)).scalars().all()

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
            "status": status,
            "counts": counts,
            "summary": {"by_channel": by_channel, "recent_new": recent_new},
            "channels": [c.value for c in ChannelEnum],
            "channel_filter": channel,
            "q": q,
            "flash": _flash(request.query_params.get("flash")),
        },
    )


@router.post("/alerts/{alert_id}/start")
async def alerts_start(
    alert_id: int,
    operator: OperatorDep,
    assignee: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
) -> Response:
    async with session.begin():
        alert = await session.get(MappingAlert, alert_id)
        if alert is None:
            return RedirectResponse(url="/admin/alerts?flash=notfound", status_code=303)
        if alert.status in _OUTSTANDING:
            alert.status = MappingAlertStatusEnum.IN_PROGRESS.value
            alert.assignee = assignee.strip() or operator
    return RedirectResponse(url="/admin/alerts?status=in_progress&flash=started", status_code=303)


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
        tab = alert.status if alert.status in _OUTSTANDING else "open"
        replayed = await MappingService(session).resolve_alert(
            channel=alert.channel,
            channel_sku=alert.channel_sku,
            marketplace_id=alert.marketplace_id,
            master_sku_id=master_sku_id,
        )
    return RedirectResponse(
        url=f"/admin/alerts?status={tab}&flash=resolved:{replayed}", status_code=303
    )


def _flash(token: str | None) -> dict[str, str] | None:
    if not token:
        return None
    parts = token.split(":")
    if parts[0] == "resolved" and len(parts) == 2:
        return {
            "kind": "ok",
            "message": f"解決しました。保留中の注文 {parts[1]} 件を再処理しました。",
        }
    if parts[0] == "started":
        return {"kind": "ok", "message": "対応中にしました。"}
    if parts[0] == "notfound":
        return {"kind": "error", "message": "アラートが見つかりません。"}
    return None
