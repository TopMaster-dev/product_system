"""Data-quality summary and the Rakuten SKU defect report.

Both screens are read-only. Their job is to turn "something is wrong somewhere"
into a number with a link, and — for the Rakuten report — into a CSV the client
can hand straight to whoever maintains RMS.
"""

from __future__ import annotations

import csv
import io
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.config import Settings, get_settings
from app.db import get_session
from app.services.data_quality import rakuten_sku_report, summary_tiles
from app.ui.auth import OperatorDep
from app.ui.deps import templates

router = APIRouter()

SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.get("/data-quality")
async def data_quality_summary(
    request: Request,
    operator: OperatorDep,
    settings: SettingsDep,
    session: AsyncSession = Depends(get_session),
    deep: int = 0,
) -> Response:
    """Every defect class as a tile with somewhere to go and fix it.

    `?deep=1` adds the snapshot-vs-events drift count. It is off by default
    because it aggregates the whole `inventory_events` table, and this page is
    meant to be cheap enough to open on a hunch.
    """
    tiles = await summary_tiles(
        session,
        placeholder_pattern=settings.rakuten_placeholder_sku_pattern,
        deep=bool(deep),
    )
    return templates.TemplateResponse(
        request,
        "data_quality.html",
        {
            "operator": operator,
            "version": __version__,
            "tiles": tiles,
            "deep": bool(deep),
        },
    )


@router.get("/data-quality/rakuten")
async def rakuten_report(
    request: Request,
    operator: OperatorDep,
    settings: SettingsDep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    report = await rakuten_sku_report(
        session, placeholder_pattern=settings.rakuten_placeholder_sku_pattern
    )
    return templates.TemplateResponse(
        request,
        "data_quality_rakuten.html",
        {
            "operator": operator,
            "version": __version__,
            "report": report,
            "pattern": settings.rakuten_placeholder_sku_pattern,
        },
    )


@router.get("/data-quality/rakuten/export.csv")
async def rakuten_report_csv(
    operator: OperatorDep,
    settings: SettingsDep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """The RMS correction request, as a file.

    One row per defect with the SKU管理番号, what is wrong, and which product it
    should identify — everything the person editing RMS needs, without them
    having to cross-reference our screens. CP932 because it is opened in Excel
    on a Windows desktop, and a mojibake'd request gets ignored.
    """
    report = await rakuten_sku_report(
        session, placeholder_pattern=settings.rakuten_placeholder_sku_pattern
    )
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["区分", "SKU管理番号", "商品管理番号", "対象SKU", "商品名", "件数", "数量", "金額", "備考"]
    )
    for row in report.placeholder:
        writer.writerow(
            [
                "仮値",
                row["channel_sku"],
                row["channel_product_id"],
                row["master_sku_code"],
                row["product_name"],
                "",
                "",
                "",
                "RMSで実際のSKUコードに変更してください",
            ]
        )
    for row in report.duplicated:
        writer.writerow(
            [
                "重複",
                row["channel_sku"],
                "",
                row["masters"],
                "",
                row["mappings"],
                "",
                "",
                "同じSKU管理番号が複数商品に割り当てられています",
            ]
        )
    for row in report.unmapped_examples:
        writer.writerow(
            [
                "未マッピング実績",
                row["channel_sku"],
                "",
                "",
                "",
                row["lines"],
                row["quantity"],
                row["amount"],
                "受注実績はあるがマスターに紐づいていません",
            ]
        )
    buf.seek(0)
    return StreamingResponse(
        # CP932 with replacement: a stray character must not fail the download
        # of a file whose whole purpose is to get the data fixed.
        iter([buf.getvalue().encode("cp932", errors="replace")]),
        media_type="text/csv; charset=Shift_JIS",
        headers={"Content-Disposition": 'attachment; filename="rakuten_sku_defects.csv"'},
    )
