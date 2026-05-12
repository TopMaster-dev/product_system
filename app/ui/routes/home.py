"""Admin dashboard."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.db import get_session
from app.models import (
    ChannelSkuMapping,
    InventoryEvent,
    MappingAlert,
    MappingAlertStatusEnum,
    MasterSku,
)
from app.ui.auth import OperatorDep
from app.ui.deps import templates

router = APIRouter()


@router.get("/")
async def home(
    request: Request,
    operator: OperatorDep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    master_count = await session.scalar(select(func.count()).select_from(MasterSku))
    mapping_count = await session.scalar(select(func.count()).select_from(ChannelSkuMapping))
    open_alerts = await session.scalar(
        select(func.count())
        .select_from(MappingAlert)
        .where(MappingAlert.status == MappingAlertStatusEnum.OPEN)
    )
    today = datetime.now(UTC) - timedelta(hours=24)
    events_today = await session.scalar(
        select(func.count()).select_from(InventoryEvent).where(InventoryEvent.occurred_at >= today)
    )

    return templates.TemplateResponse(
        request,
        "home.html",
        {
            "operator": operator,
            "version": __version__,
            "stats": {
                "master_skus": master_count or 0,
                "mappings": mapping_count or 0,
                "open_alerts": open_alerts or 0,
                "events_today": events_today or 0,
            },
        },
    )
