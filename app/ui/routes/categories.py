"""Category master — CRUD plus the SKU-assignment CSV import.

The CSV import is a four-stage flow: upload → 検証 → 確認 → 実行. The raw file
rides through the confirm step in a base64 hidden field rather than being staged
on disk or in a session, because Cloud Run gives no shared writable storage and
two instances would not see each other's temp files.

The file is re-validated at execute time, never trusted from the preview. The
preview proves what the file said a moment ago; the database may have changed
since, and the hidden field is client-controlled input either way.
"""

from __future__ import annotations

import base64
import binascii
import csv
import io
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.db import get_session
from app.logging import get_logger
from app.models import MAX_CATEGORY_LEVEL, MasterSku, ProductCategory
from app.services.categories import (
    CategoryError,
    assert_can_deactivate,
    assert_can_delete,
    load_overview,
    plan_assignments,
)
from app.services.sku_scope import operational_conditions
from app.ui.auth import OperatorDep
from app.ui.csv_export import csv_response
from app.ui.deps import templates

router = APIRouter()
log = get_logger(__name__)


@router.get("/categories")
async def categories_list(
    request: Request,
    operator: OperatorDep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    overview = await load_overview(session)
    return templates.TemplateResponse(
        request,
        "categories.html",
        {
            "operator": operator,
            "version": __version__,
            "overview": overview,
            "max_level": MAX_CATEGORY_LEVEL,
            "flash": _flash(request.query_params.get("flash")),
        },
    )


@router.get("/categories/export.csv")
async def categories_export(
    operator: OperatorDep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """The taxonomy as the client sent it, so they can edit and re-send."""
    overview = await load_overview(session)
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["category_code", "大分類", "中分類", "有効", "割当SKU数"])
    for root in overview.roots:
        writer.writerow(
            [
                root.category.code,
                root.category.name,
                "",
                "はい" if root.category.is_active else "",
                root.total_sku_count,
            ]
        )
        for child in root.children:
            writer.writerow(
                [
                    child.category.code,
                    root.category.name,
                    child.category.name,
                    "はい" if child.category.is_active else "",
                    child.total_sku_count,
                ]
            )
    return csv_response(buf.getvalue(), filename="categories.csv")


@router.post("/categories")
async def category_create(
    operator: OperatorDep,
    code: Annotated[str, Form()],
    name: Annotated[str, Form()],
    parent_id: Annotated[str, Form()] = "",
    sort_order: Annotated[int, Form()] = 0,
    session: AsyncSession = Depends(get_session),
) -> Response:
    code, name = code.strip(), name.strip()
    if not code or not name:
        return _back("invalid")
    parent = int(parent_id) if parent_id.strip() else None
    category = ProductCategory(
        code=code,
        name=name,
        parent_id=parent,
        # The CHECK enforces this pairing; deriving it here means the form never
        # has to ask, and a 中分類 cannot be filed at the wrong level.
        level=1 if parent is None else 2,
        sort_order=sort_order,
    )
    session.add(category)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        log.info("categories.create_conflict", code=code, error=str(exc.orig)[:200])
        return _back("duplicate")
    log.info("categories.created", code=code, level=category.level, operator=operator)
    return _back("created")


# --------------------------------------------------------------------------
# SKU assignment CSV: upload -> 検証 -> 確認 -> 実行
# --------------------------------------------------------------------------


@router.get("/categories/upload")
async def upload_form(
    request: Request,
    operator: OperatorDep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    overview = await load_overview(session)
    return templates.TemplateResponse(
        request,
        "categories_upload.html",
        {
            "operator": operator,
            "version": __version__,
            "overview": overview,
            "flash": _flash(request.query_params.get("flash")),
        },
    )


@router.post("/categories/upload")
async def upload_preview(
    request: Request,
    operator: OperatorDep,
    file: Annotated[UploadFile, File()],
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Validate and show what WOULD change. Writes nothing."""
    data = await file.read()
    inspection, plan = await plan_assignments(session, data)
    return templates.TemplateResponse(
        request,
        "categories_preview.html",
        {
            "operator": operator,
            "version": __version__,
            "inspection": inspection,
            "plan": plan,
            "filename": file.filename or "sku_categories.csv",
            # Carried to the confirm step. Cloud Run has no shared writable
            # storage, so staging the file on disk would strand it on whichever
            # instance handled the upload.
            "csv_b64": base64.b64encode(data).decode("ascii"),
        },
    )


@router.post("/categories/upload/apply")
async def upload_apply(
    operator: OperatorDep,
    csv_b64: Annotated[str, Form()],
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Re-validate from scratch, then write in a single transaction.

    Deliberately does NOT trust the preview: it is client-controlled input, and
    the categories it resolved against may have changed in between.
    """
    try:
        data = base64.b64decode(csv_b64, validate=True)
    except (binascii.Error, ValueError):
        return _back("invalid", path="/admin/categories/upload")

    inspection, plan = await plan_assignments(session, data)
    if inspection.fatal:
        return _back("invalid", path="/admin/categories/upload")

    applied = 0
    for category_id, sku_ids in plan.by_category.items():
        if not sku_ids:
            continue
        await session.execute(
            update(MasterSku).where(MasterSku.id.in_(sku_ids)).values(category_id=category_id)
        )
        applied += len(sku_ids)
    await session.commit()

    log.info(
        "categories.import_applied",
        assigned=applied,
        cleared=len(plan.by_category.get(None, [])),
        unknown_skus=len(plan.unknown_skus),
        unknown_categories=len(plan.unknown_categories),
        operator=operator,
    )
    return _back("imported")


@router.post("/categories/{category_id}")
async def category_update(
    operator: OperatorDep,
    category_id: int,
    name: Annotated[str, Form()] = "",
    sort_order: Annotated[int, Form()] = 0,
    session: AsyncSession = Depends(get_session),
) -> Response:
    category = await session.get(ProductCategory, category_id)
    if category is None:
        return _back("notfound")
    if name.strip():
        category.name = name.strip()
    category.sort_order = sort_order
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        return _back("duplicate")
    log.info("categories.updated", code=category.code, operator=operator)
    return _back("updated")


@router.post("/categories/{category_id}/toggle")
async def category_toggle(
    operator: OperatorDep,
    category_id: int,
    session: AsyncSession = Depends(get_session),
) -> Response:
    category = await session.get(ProductCategory, category_id)
    if category is None:
        return _back("notfound")
    if category.is_active:
        try:
            await assert_can_deactivate(session, category)
        except CategoryError:
            return _back("has_children")
    category.is_active = not category.is_active
    await session.commit()
    log.info(
        "categories.toggled",
        code=category.code,
        is_active=category.is_active,
        operator=operator,
    )
    return _back("enabled" if category.is_active else "disabled")


@router.post("/categories/{category_id}/delete")
async def category_delete(
    operator: OperatorDep,
    category_id: int,
    session: AsyncSession = Depends(get_session),
) -> Response:
    category = await session.get(ProductCategory, category_id)
    if category is None:
        return _back("notfound")
    try:
        await assert_can_delete(session, category)
    except CategoryError:
        return _back("in_use")
    await session.delete(category)
    await session.commit()
    log.info("categories.deleted", code=category.code, operator=operator)
    return _back("deleted")


def _back(flash: str, *, path: str = "/admin/categories") -> RedirectResponse:
    return RedirectResponse(url=f"{path}?{urlencode({'flash': flash})}", status_code=303)


def _flash(token: str | None) -> dict[str, str] | None:
    table = {
        "created": ("ok", "カテゴリを追加しました。"),
        "updated": ("ok", "カテゴリを更新しました。"),
        "deleted": ("ok", "カテゴリを削除しました。"),
        "enabled": ("ok", "カテゴリを有効にしました。"),
        "disabled": ("ok", "カテゴリを無効にしました。"),
        "imported": ("ok", "SKUのカテゴリを一括設定しました。"),
        "duplicate": ("error", "同じコード、または同じ階層に同名のカテゴリが既に存在します。"),
        "has_children": (
            "error",
            "有効な中分類が残っているため無効化できません。先に中分類を無効化してください。",
        ),
        "in_use": (
            "error",
            "SKUが割り当てられている、または中分類があるため削除できません。無効化をご検討ください。",
        ),
        "invalid": ("error", "入力内容を確認してください。"),
        "notfound": ("error", "指定したカテゴリが見つかりません。"),
    }
    if not token or token not in table:
        return None
    kind, message = table[token]
    return {"kind": kind, "message": message}


async def unclassified_sku_codes(session: AsyncSession, limit: int = 50) -> list[str]:
    """Sample of 未分類 SKUs, so the upload screen can show what is waiting."""
    rows: Any = await session.execute(
        select(MasterSku.sku_code)
        .where(MasterSku.category_id.is_(None), *operational_conditions())
        .order_by(MasterSku.sku_code)
        .limit(limit)
    )
    return [code for (code,) in rows.all()]
