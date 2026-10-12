"""Data-quality summary and the Rakuten SKU defect report.

Both screens are read-only. Their job is to turn "something is wrong somewhere"
into a number with a link, and — for the Rakuten report — into a CSV the client
can hand straight to whoever maintains RMS.
"""

from __future__ import annotations

import csv
import io
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.config import Settings, get_settings
from app.csv_export import csv_response
from app.db import get_session
from app.services.data_quality import (
    SKU_LIST_KINDS,
    SKU_LIST_LIMIT,
    rakuten_sku_report,
    sku_list,
    summary_tiles,
)
from app.services.sku_links import components_of
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


@router.get("/data-quality/skus")
async def data_quality_skus(
    request: Request,
    operator: OperatorDep,
    kind: str,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """The products behind one tile, with the way to where the work is done.

    The tiles used to open the work screen itself, which did not say which
    products were affected (acceptance Q&A, 2026-10-12).
    """
    meta = SKU_LIST_KINDS.get(kind)
    if meta is None:
        raise HTTPException(status_code=404, detail="unknown list")
    total, rows = await sku_list(session, kind)
    components = await components_of(session, [r["id"] for r in rows]) if kind == "bundles" else {}
    return templates.TemplateResponse(
        request,
        "data_quality_skus.html",
        {
            "operator": operator,
            "version": __version__,
            "meta": meta,
            "total": total,
            "rows": rows,
            "limit": SKU_LIST_LIMIT,
            "components": components,
        },
    )


@router.get("/data-quality/skus/export.csv")
async def data_quality_skus_csv(
    operator: OperatorDep,
    kind: str,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Every row, not the page's first 500. Leads with `sku_code` and an empty
    `category_code`, so the カテゴリ未設定 list goes straight back into the
    category import once that column is filled in."""
    meta = SKU_LIST_KINDS.get(kind)
    if meta is None:
        raise HTTPException(status_code=404, detail="unknown list")
    _, rows = await sku_list(session, kind, limit=None)
    components = await components_of(session, [r["id"] for r in rows]) if kind == "bundles" else {}
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["sku_code", "category_code", "商品名", "現在のカテゴリ", "構成品"])
    for r in rows:
        writer.writerow(
            [
                r["sku_code"],
                "",
                r["name"],
                r["category_name"] or "未分類",
                " / ".join(f"{c.sku_code} x{c.quantity_per}" for c in components.get(r["id"], [])),
            ]
        )
    return csv_response(buf.getvalue(), filename=f"data_quality_{kind}.csv")


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

    One row per defect, identifying the product the way that defect's own source
    identifies it. 仮値 and 重複 come from mappings, which for Rakuten are keyed
    on the SKU管理番号. 未マッピング実績 comes from order lines, and Rakuten
    gives us those keyed on the 商品管理番号 only (`adapters/rakuten.py`) — there
    is no SKU-level identifier in the payload at all.

    Filing the latter under the SKU管理番号 heading sent the client hunting for
    SKU values that were never in the file, so each row now fills the column its
    identifier actually belongs to. Encoding is UTF-8 + BOM via `csv_response`.
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
                "RMSでの修正は不要です。当システムはSKU管理番号を参照していません",
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
                "",
                row["channel_sku"],
                "",
                "",
                row["lines"],
                row["quantity"],
                row["amount"],
                "受注実績はあるがマスターに紐づいていません",
            ]
        )
    return csv_response(buf.getvalue(), filename="rakuten_sku_defects.csv")
