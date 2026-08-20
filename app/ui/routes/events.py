"""Inventory event log viewer with filters.

Two timestamps, and the difference between them is the point:

`occurred_at`  when the movement is considered to have happened (business time)
`created_at`   when the row was actually inserted (system time)

They diverge for BACKDATED events, and production is full of them: until the
2026-08-20 fix, a cancellation was stamped with the ORIGINAL ORDER's time, so a
credit written in August carries a June date. Showing only `occurred_at` makes
such a row indistinguishable from a genuine June movement — which is exactly the
question this screen exists to answer, and it could not. Both are shown, and a
遡及 badge marks the rows where they disagree.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, time, timedelta
from typing import Any
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

#: How far `created_at` may exceed `occurred_at` before a row counts as
#: backdated. Ordinary events are inserted within seconds of the time they claim;
#: an hour of slack absorbs queue delay and retries without hiding a genuinely
#: backdated correction, which is measured in days or months.
BACKDATE_THRESHOLD = timedelta(hours=1)


def is_backdated(event: Mapping[str, Any]) -> bool:
    """True when the row landed materially later than the time it claims.

    Passed into the template rather than precomputed per row because the query
    returns immutable RowMappings; the template asks per row.
    """
    occurred, created = event.get("occurred_at"), event.get("created_at")
    if occurred is None or created is None:
        return False
    return bool(created - occurred > BACKDATE_THRESHOLD)


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
    backdated: int = 0,
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
            InventoryEvent.created_at,
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
    if backdated:
        # Uses ix_inventory_events_created_at (migration 0009). Auditing the
        # pre-2026-08-20 cancellation damage means asking exactly this question:
        # which rows landed materially later than the date they claim?
        stmt = stmt.where(
            InventoryEvent.created_at - InventoryEvent.occurred_at > BACKDATE_THRESHOLD
        )

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
    if backdated:
        base["backdated"] = 1
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
            "backdated": bool(backdated),
            "is_backdated": is_backdated,
            "pagination": pagination,
        },
    )
