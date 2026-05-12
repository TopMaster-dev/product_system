"""Inventory event log viewer with filters."""

from __future__ import annotations

from datetime import UTC, datetime, time
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.db import get_session
from app.models import ChannelEnum, InventoryEvent, InventoryEventTypeEnum, MasterSku
from app.ui.auth import OperatorDep
from app.ui.deps import templates

router = APIRouter()

PAGE_SIZE = 100


@router.get("/events")
async def events_list(
    request: Request,
    operator: OperatorDep,
    session: AsyncSession = Depends(get_session),
    sku_code: str = "",
    event_type: str = "",
    channel: str = "",
    since: str = "",
    until: str = "",
    master_sku_id: int | None = None,
    offset: int = 0,
) -> Response:
    stmt = (
        select(
            InventoryEvent.id,
            InventoryEvent.event_type,
            InventoryEvent.quantity_delta,
            InventoryEvent.source_channel,
            InventoryEvent.source_order_id,
            InventoryEvent.source_line_id,
            InventoryEvent.reason,
            InventoryEvent.operator,
            InventoryEvent.occurred_at,
            MasterSku.sku_code,
        )
        .join(MasterSku, MasterSku.id == InventoryEvent.master_sku_id)
        .order_by(InventoryEvent.occurred_at.desc())
    )

    if sku_code:
        stmt = stmt.where(MasterSku.sku_code.ilike(f"%{sku_code}%"))
    if event_type:
        stmt = stmt.where(InventoryEvent.event_type == event_type)
    if channel:
        stmt = stmt.where(InventoryEvent.source_channel == channel)
    if since:
        try:
            d = datetime.fromisoformat(since).replace(tzinfo=UTC)
            stmt = stmt.where(InventoryEvent.occurred_at >= d)
        except ValueError:
            pass
    if until:
        try:
            d = datetime.combine(datetime.fromisoformat(until).date(), time.max, tzinfo=UTC)
            stmt = stmt.where(InventoryEvent.occurred_at <= d)
        except ValueError:
            pass
    if master_sku_id is not None:
        stmt = stmt.where(InventoryEvent.master_sku_id == master_sku_id)

    stmt = stmt.offset(offset).limit(PAGE_SIZE + 1)
    events = (await session.execute(stmt)).mappings().all()
    has_next = len(events) > PAGE_SIZE
    events = events[:PAGE_SIZE]

    base: dict[str, str | int] = {
        "sku_code": sku_code,
        "event_type": event_type,
        "channel": channel,
        "since": since,
        "until": until,
    }
    if master_sku_id is not None:
        base["master_sku_id"] = master_sku_id
    pagination = {
        "has_prev": offset > 0,
        "has_next": has_next,
        "qs_prev": urlencode({**base, "offset": max(0, offset - PAGE_SIZE)}),
        "qs_next": urlencode({**base, "offset": offset + PAGE_SIZE}),
    }

    return templates.TemplateResponse(
        request,
        "events.html",
        {
            "operator": operator,
            "version": __version__,
            "events": events,
            "filters": {
                "sku_code": sku_code,
                "event_type": event_type,
                "channel": channel,
                "since": since,
                "until": until,
            },
            "event_types": [t.value for t in InventoryEventTypeEnum],
            "channels": [c.value for c in ChannelEnum],
            "pagination": pagination,
        },
    )
