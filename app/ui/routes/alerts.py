"""Mapping alerts — 3-state workflow (未対応 / 対応中 / 解決済み), count badges,
channel/SKU filter, a status summary, and inline resolution.

WHY THE SKU PICKER IS A DATALIST, NOT A SELECT PER ROW
------------------------------------------------------
Each row needs a "which master does this belong to?" picker. Rendering a
`<select>` of every master inside every row is O(alerts x masters), and on
2026-08-25 that reached 156 x 1051 = 163,956 `<option>` elements — a **28.4 MiB**
response against Cloud Run's 32 MiB cap, taking ~2s to render on a machine much
faster than a 1 vCPU container. The page returned HTTP 500 with no traceback in
the logs, because the app never raised: the response itself was the problem.

It passed every test and worked for months, because a test database has a
handful of masters and the catalogue only crossed 1,000 after the variant
cutover. The cost was quadratic all along.

So the master list is emitted ONCE as a `<datalist>` that every row references,
the rows submit `sku_code` (resolved server-side), and the list is scoped to
`operational_conditions()`. Same page: well under 1 MiB. The alert list is also
paginated, so the 解決済 tab cannot grow into the same wall.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.db import get_session
from app.models import ChannelEnum, MappingAlert, MappingAlertStatusEnum, MasterSku
from app.services import MappingService
from app.services.sku_scope import operational_conditions
from app.ui.auth import OperatorDep
from app.ui.deps import templates

router = APIRouter()

_TABS = (
    MappingAlertStatusEnum.OPEN.value,
    MappingAlertStatusEnum.IN_PROGRESS.value,
    MappingAlertStatusEnum.RESOLVED.value,
)
_OUTSTANDING = [MappingAlertStatusEnum.OPEN.value, MappingAlertStatusEnum.IN_PROGRESS.value]

#: The 解決済 tab grows without bound; an unpaginated list is how this screen
#: walks back into the same wall from the other direction.
PAGE_SIZE = 50


@router.get("/alerts")
async def alerts_list(
    request: Request,
    operator: OperatorDep,
    session: AsyncSession = Depends(get_session),
    status: str = MappingAlertStatusEnum.OPEN.value,
    channel: str = "",
    q: str = "",
    offset: int = 0,
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
    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0
    stmt = stmt.order_by(MappingAlert.first_seen_at.desc()).offset(offset).limit(PAGE_SIZE)
    alerts = (await session.execute(stmt)).scalars().all()

    base_qs = {"status": status, "channel": channel, "q": q}

    # Scoped, not every master. Archived and 在庫管理対象外 SKUs are not valid
    # targets for a new mapping — offering the 359 masters the cutover retired
    # invites resolving an alert onto a dead product.
    master_skus = (
        (
            await session.execute(
                select(MasterSku).where(*operational_conditions()).order_by(MasterSku.sku_code)
            )
        )
        .scalars()
        .all()
    )
    # Keyed by sku_code because that is what the picker now submits.
    sku_images = {s.sku_code: s.image_url for s in master_skus if s.image_url}
    return templates.TemplateResponse(
        request,
        "alerts.html",
        {
            "operator": operator,
            "version": __version__,
            "alerts": alerts,
            "master_skus": master_skus,
            "sku_images": sku_images,
            "status": status,
            "counts": counts,
            "summary": {"by_channel": by_channel, "recent_new": recent_new},
            "channels": [c.value for c in ChannelEnum],
            "channel_filter": channel,
            "q": q,
            "pagination": {
                "total": total,
                "offset": offset,
                "shown": len(alerts),
                "has_prev": offset > 0,
                "has_next": offset + PAGE_SIZE < total,
                "qs_prev": urlencode({**base_qs, "offset": max(0, offset - PAGE_SIZE)}),
                "qs_next": urlencode({**base_qs, "offset": offset + PAGE_SIZE}),
            },
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
    sku_code: Annotated[str, Form()],
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Resolve by sku_code, which is what the datalist picker submits.

    A `<datalist>` is a suggestion list, not a constraint — the browser will
    happily submit whatever was typed. So the code is resolved here and an
    unknown one is rejected, rather than trusted.
    """
    async with session.begin():
        alert = await session.get(MappingAlert, alert_id)
        if alert is None:
            return RedirectResponse(url="/admin/alerts?flash=notfound", status_code=303)
        master_sku_id = await session.scalar(
            select(MasterSku.id).where(MasterSku.sku_code == sku_code.strip())
        )
        if master_sku_id is None:
            return RedirectResponse(
                url=f"/admin/alerts?status={alert.status}&flash=badsku", status_code=303
            )
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
    if parts[0] == "badsku":
        return {
            "kind": "error",
            "message": "そのSKUコードは存在しません。候補から選択してください。",
        }
    return None
