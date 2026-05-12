"""SKU mapping CRUD + CSV import/export."""

from __future__ import annotations

import csv
import io
from typing import Annotated

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.db import get_session
from app.models import ChannelEnum, ChannelSkuMapping, MasterSku
from app.ui.auth import OperatorDep
from app.ui.deps import templates

router = APIRouter(prefix="/mappings")


@router.get("")
async def mappings_list(
    request: Request,
    operator: OperatorDep,
    session: AsyncSession = Depends(get_session),
    q: str = "",
    channel: str = "",
) -> Response:
    stmt = (
        select(
            ChannelSkuMapping.id,
            ChannelSkuMapping.channel,
            ChannelSkuMapping.channel_sku,
            ChannelSkuMapping.channel_product_id,
            ChannelSkuMapping.is_active,
            MasterSku.sku_code.label("master_sku_code"),
            MasterSku.name.label("master_sku_name"),
        )
        .join(MasterSku, MasterSku.id == ChannelSkuMapping.master_sku_id)
        .order_by(ChannelSkuMapping.channel, ChannelSkuMapping.channel_sku)
    )
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(MasterSku.sku_code.ilike(like), ChannelSkuMapping.channel_sku.ilike(like))
        )
    if channel:
        stmt = stmt.where(ChannelSkuMapping.channel == channel)

    rows = (await session.execute(stmt)).mappings().all()

    flash = request.query_params.get("flash")
    return templates.TemplateResponse(
        request,
        "mappings_list.html",
        {
            "operator": operator,
            "version": __version__,
            "rows": rows,
            "q": q,
            "channels": [c.value for c in ChannelEnum],
            "channel_filter": channel,
            "flash": _flash(flash),
        },
    )


@router.get("/new")
async def mappings_new_form(
    request: Request,
    operator: OperatorDep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    skus = (await session.execute(select(MasterSku).order_by(MasterSku.sku_code))).scalars().all()
    return templates.TemplateResponse(
        request,
        "mappings_new.html",
        {"operator": operator, "version": __version__, "master_skus": skus},
    )


@router.post("/new")
async def mappings_new_submit(
    operator: OperatorDep,
    master_sku_id: Annotated[int, Form()],
    channel: Annotated[str, Form()],
    channel_sku: Annotated[str, Form()],
    channel_product_id: Annotated[str, Form()] = "",
    marketplace_id: Annotated[str, Form()] = "",
    session: AsyncSession = Depends(get_session),
) -> Response:
    async with session.begin():
        mapping = ChannelSkuMapping(
            master_sku_id=master_sku_id,
            channel=channel,
            channel_sku=channel_sku.strip(),
            channel_product_id=(channel_product_id.strip() or None),
            marketplace_id=(marketplace_id.strip() or None),
            is_active=True,
        )
        session.add(mapping)
        try:
            await session.flush()
        except IntegrityError:
            await session.rollback()
            return RedirectResponse(
                url="/admin/mappings?flash=duplicate",
                status_code=303,
            )
    return RedirectResponse(url="/admin/mappings?flash=created", status_code=303)


@router.post("/{mapping_id}/delete")
async def mappings_delete(
    mapping_id: int,
    operator: OperatorDep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    async with session.begin():
        row = await session.get(ChannelSkuMapping, mapping_id)
        if row is not None:
            await session.delete(row)
    return RedirectResponse(url="/admin/mappings?flash=deleted", status_code=303)


@router.get("/export.csv")
async def mappings_export(
    operator: OperatorDep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    rows = (
        await session.execute(
            select(
                MasterSku.sku_code,
                ChannelSkuMapping.channel,
                ChannelSkuMapping.channel_sku,
                ChannelSkuMapping.channel_product_id,
                ChannelSkuMapping.marketplace_id,
                ChannelSkuMapping.is_active,
            ).join(MasterSku, MasterSku.id == ChannelSkuMapping.master_sku_id)
        )
    ).all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "master_sku_code",
            "channel",
            "channel_sku",
            "channel_product_id",
            "marketplace_id",
            "is_active",
        ]
    )
    for r in rows:
        writer.writerow([r[0], r[1], r[2], r[3] or "", r[4] or "", "1" if r[5] else "0"])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": 'attachment; filename="mappings.csv"'},
    )


@router.post("/import")
async def mappings_import(
    operator: OperatorDep,
    file: Annotated[UploadFile, File()],
    session: AsyncSession = Depends(get_session),
) -> Response:
    text = (await file.read()).decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))

    inserted = 0
    skipped = 0
    async with session.begin():
        sku_map = {
            row.sku_code: row.id
            for row in (await session.execute(select(MasterSku))).scalars().all()
        }
        for row in reader:
            code = (row.get("master_sku_code") or "").strip()
            master_id = sku_map.get(code)
            if master_id is None:
                skipped += 1
                continue
            mapping = ChannelSkuMapping(
                master_sku_id=master_id,
                channel=(row.get("channel") or "").strip(),
                channel_sku=(row.get("channel_sku") or "").strip(),
                channel_product_id=(row.get("channel_product_id") or "").strip() or None,
                marketplace_id=(row.get("marketplace_id") or "").strip() or None,
                is_active=True,
            )
            try:
                async with session.begin_nested():
                    session.add(mapping)
                    await session.flush()
                inserted += 1
            except IntegrityError:
                skipped += 1

    return RedirectResponse(
        url=f"/admin/mappings?flash=imported:{inserted}:{skipped}", status_code=303
    )


def _flash(token: str | None) -> dict[str, str] | None:
    if not token:
        return None
    parts = token.split(":")
    head = parts[0]
    if head == "created":
        return {"kind": "ok", "message": "マッピングを登録しました。"}
    if head == "deleted":
        return {"kind": "ok", "message": "マッピングを削除しました。"}
    if head == "duplicate":
        return {
            "kind": "error",
            "message": "同じ channel と channel_sku のマッピングが既に存在します。",
        }
    if head == "imported" and len(parts) == 3:
        return {
            "kind": "ok",
            "message": f"CSV取込完了: 追加 {parts[1]} 件 / スキップ {parts[2]} 件",
        }
    return None
