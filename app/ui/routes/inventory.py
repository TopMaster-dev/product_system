"""Inventory list — search, status buckets, sorting, count badges,
best-seller flagging, and CSV export.

One structural rule holds this screen together: **the row query, the badge
counts and the CSV export are built from the SAME predicate list**
(`_scope_conditions`). They used to be written separately, which is how a badge
can claim "低在庫 38" while the 低在庫 view shows a different number — the class
of bug that erodes trust in every other figure on the page.

Stock-bucket logic lives in `app.services.stock_status`; the SKU population
predicate lives in `app.services.sku_scope`. Neither is re-derived here.
"""

from __future__ import annotations

import csv
import io
import math
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import ColumnElement, Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.config import Settings, get_settings
from app.db import get_session
from app.logging import get_logger
from app.models import (
    InventoryEvent,
    InventoryEventTypeEnum,
    InventorySnapshot,
    MasterSku,
    SkuVelocity,
)
from app.services.sku_scope import (
    analysable_conditions,
    archive_blockers,
    operational_conditions,
)
from app.services.stock_status import (
    STATUS_LABELS,
    StockStatus,
    classify,
    count_expression,
    filter_condition,
    qty_expression,
    status_rank,
)
from app.ui.auth import OperatorDep
from app.ui.csv_export import csv_response
from app.ui.deps import templates

router = APIRouter()
log = get_logger(__name__)

PAGE_SIZE = 50

SettingsDep = Annotated[Settings, Depends(get_settings)]

# Filter values that are not stock buckets.
_FILTER_ALL = "all"
_FILTER_BESTSELLER = "bestseller"


async def best_seller_ids(
    session: AsyncSession,
    *,
    window_days: int,
    top_percent: int,
) -> set[int]:
    """master_sku_ids in the top `top_percent`% by consumed quantity over the
    trailing `window_days`.

    Scoped to the analysable population: without the join, packaging and coupon
    SKUs dominate the ranking because they "sell" with every single order — in
    Phase 1-B a gift box was the top seller. Bundle parents are excluded too,
    since their sales fan out to components and would be double-counted.
    """
    since = datetime.now(UTC) - timedelta(days=window_days)
    consumed = (
        select(
            InventoryEvent.master_sku_id.label("mid"),
            func.sum(func.abs(InventoryEvent.quantity_delta)).label("qty"),
        )
        .join(MasterSku, MasterSku.id == InventoryEvent.master_sku_id)
        .where(
            InventoryEvent.event_type == InventoryEventTypeEnum.ORDER_CONSUMED,
            InventoryEvent.occurred_at >= since,
            *analysable_conditions(include_archived=False),
        )
        .group_by(InventoryEvent.master_sku_id)
        .subquery()
    )
    total = await session.scalar(select(func.count()).select_from(consumed)) or 0
    if total == 0:
        return set()
    limit = max(1, math.ceil(total * top_percent / 100))
    rows = await session.execute(
        select(consumed.c.mid).order_by(consumed.c.qty.desc()).limit(limit)
    )
    return {mid for (mid,) in rows.all()}


def _scope_conditions(q: str, *, include_hidden: bool) -> list[ColumnElement[bool]]:
    """The SKU population this screen covers: the search term plus the
    operational scope. Every query on this page starts from exactly this list."""
    conditions: list[ColumnElement[bool]] = list(
        operational_conditions(include_archived=include_hidden, include_unmanaged=include_hidden)
    )
    if q:
        like = f"%{q}%"
        conditions.append(or_(MasterSku.sku_code.ilike(like), MasterSku.name.ilike(like)))
    return conditions


def _from_masters(*selected: Any) -> Select[Any]:
    """Every query on this page starts here, including the badge counts.

    `sku_velocity` is joined for ALL of them, because the low-stock threshold is
    now per-SKU (P2-017) and a badge resolving it differently from the list it
    labels is the exact defect this screen has had before.
    """
    return (
        select(*selected)
        .select_from(MasterSku)
        .outerjoin(InventorySnapshot, InventorySnapshot.master_sku_id == MasterSku.id)
        .outerjoin(SkuVelocity, SkuVelocity.master_sku_id == MasterSku.id)
    )


def _threshold_expression(fallback: int) -> ColumnElement[int]:
    """The per-SKU low-stock threshold, with a floor for SKUs that have none.

    A missing row means the rollup has not reached this SKU yet — a brand-new
    master, or the first hours after deploy. Falling back to the configured
    fixed value keeps the screen behaving exactly as it did before 0013 rather
    than treating an unknown rate as a safe one.
    """
    return func.coalesce(SkuVelocity.low_stock_threshold, fallback)


def _rows_query(
    conditions: list[ColumnElement[bool]], threshold: ColumnElement[int]
) -> Select[Any]:
    """The row set, carrying each SKU's own threshold.

    Selected rather than recomputed per row in Python: the CSV classifies rows
    with `classify()`, and a second definition of "which threshold applies here"
    would eventually disagree with the one the WHERE clause used — a file whose
    status column contradicts the filter that produced it.
    """
    return _from_masters(
        MasterSku.id,
        MasterSku.sku_code,
        MasterSku.name,
        MasterSku.jan_code,
        MasterSku.image_url,
        MasterSku.archived_at,
        MasterSku.is_stock_managed,
        qty_expression().label("on_hand_qty"),
        threshold.label("low_stock_threshold"),
        SkuVelocity.per_day.label("velocity_per_day"),
        InventorySnapshot.updated_at,
    ).where(*conditions)


def _apply_bucket(
    stmt: Select[Any],
    filter_mode: str,
    best_sellers: set[int],
    threshold: ColumnElement[int],
) -> Select[Any]:
    if filter_mode == _FILTER_BESTSELLER:
        return stmt.where(MasterSku.id.in_(best_sellers or {-1}))
    for status in StockStatus:
        if filter_mode == status.value:
            return stmt.where(filter_condition(status, threshold))
    return stmt


def _sort_columns(threshold: ColumnElement[int]) -> dict[str, Any]:
    """Built per request. The threshold is a joined column since 0013, so the
    sort key cannot be a module-level constant."""
    return {
        "status": status_rank(threshold),
        "sku": MasterSku.sku_code,
        "name": MasterSku.name,
        "qty": qty_expression(),
        "updated": InventorySnapshot.updated_at,
    }


#: Validation of the `sort` query parameter must not depend on settings.
_SORT_KEYS = frozenset({"status", "sku", "name", "qty", "updated"})


def _apply_sort(
    stmt: Select[Any], sort: str, direction: str, threshold: ColumnElement[int]
) -> Select[Any]:
    col = _sort_columns(threshold)[sort]
    ordered = col.desc() if direction == "desc" else col.asc()
    # Stable tiebreaker so equal-rank rows keep a deterministic order.
    return stmt.order_by(ordered, MasterSku.sku_code)


def _normalize(sort: str, direction: str) -> tuple[str, str]:
    if sort not in _SORT_KEYS:
        sort = "status"
    if direction not in ("asc", "desc"):
        direction = "asc"
    return sort, direction


@router.get("/inventory")
async def inventory_list(
    request: Request,
    operator: OperatorDep,
    settings: SettingsDep,
    session: AsyncSession = Depends(get_session),
    q: str = "",
    filter: str = _FILTER_ALL,
    sort: str = "status",
    dir: str = "asc",
    include_hidden: int = 0,
    offset: int = 0,
) -> Response:
    sort, dir = _normalize(sort, dir)
    threshold = _threshold_expression(settings.low_stock_threshold)
    show_hidden = bool(include_hidden)

    best_sellers = await best_seller_ids(
        session,
        window_days=settings.best_seller_window_days,
        top_percent=settings.best_seller_top_percent,
    )

    conditions = _scope_conditions(q, include_hidden=show_hidden)
    stmt = _apply_bucket(_rows_query(conditions, threshold), filter, best_sellers, threshold)
    total = await session.scalar(select(func.count()).select_from(stmt.subquery())) or 0

    # Badges: SAME conditions as the rows, minus the bucket filter, so each badge
    # equals the row count of the view it links to.
    counts_row = (
        await session.execute(
            _from_masters(
                count_expression(StockStatus.NEGATIVE, threshold),
                count_expression(StockStatus.ZERO, threshold),
                count_expression(StockStatus.LOW, threshold),
            ).where(*conditions)
        )
    ).one()
    counts = {"negative": counts_row[0], "zero": counts_row[1], "low": counts_row[2]}

    rows = (
        (
            await session.execute(
                _apply_sort(stmt, sort, dir, threshold).offset(offset).limit(PAGE_SIZE)
            )
        )
        .mappings()
        .all()
    )

    base = {"q": q, "filter": filter, "sort": sort, "dir": dir}
    if show_hidden:
        base["include_hidden"] = "1"
    pagination = {
        "total": total,
        "offset": offset,
        "has_prev": offset > 0,
        "has_next": offset + PAGE_SIZE < total,
        "qs_prev": urlencode({**base, "offset": max(0, offset - PAGE_SIZE)}),
        "qs_next": urlencode({**base, "offset": offset + PAGE_SIZE}),
    }

    return templates.TemplateResponse(
        request,
        "inventory_list.html",
        {
            "operator": operator,
            "version": __version__,
            "rows": rows,
            "q": q,
            "filter_mode": filter,
            "sort": sort,
            "dir": dir,
            "counts": counts,
            "best_sellers": best_sellers,
            # The SCALAR fallback, not the column expression used in SQL.
            # Each row carries its own threshold; this is only for copy that
            # describes the default. Passing the ColumnElement here would make
            # every Jinja `qty < threshold` truthy and mark the whole page low.
            "threshold": settings.low_stock_threshold,
            "include_hidden": show_hidden,
            "flash": _flash(request.query_params.get("flash")),
            # Carried on the toggle POSTs so the operator lands back on the same
            # filtered page instead of a reset list.
            "base_qs": urlencode(base),
            "export_qs": urlencode(base),
            "pagination": pagination,
        },
    )


@router.get("/inventory/export.csv")
async def inventory_export(
    operator: OperatorDep,
    settings: SettingsDep,
    session: AsyncSession = Depends(get_session),
    q: str = "",
    filter: str = _FILTER_ALL,
    sort: str = "status",
    dir: str = "asc",
    include_hidden: int = 0,
) -> Response:
    """Export exactly what the screen is showing — same predicates, same order."""
    sort, dir = _normalize(sort, dir)
    threshold = _threshold_expression(settings.low_stock_threshold)

    best_sellers = await best_seller_ids(
        session,
        window_days=settings.best_seller_window_days,
        top_percent=settings.best_seller_top_percent,
    )
    conditions = _scope_conditions(q, include_hidden=bool(include_hidden))
    stmt = _apply_bucket(_rows_query(conditions, threshold), filter, best_sellers, threshold)
    rows = (await session.execute(_apply_sort(stmt, sort, dir, threshold))).mappings().all()

    buf = io.StringIO()
    writer = csv.writer(buf)
    # New columns are APPENDED, never inserted: operators have saved Excel
    # sheets and filters built on the Phase 1-B column positions.
    writer.writerow(
        [
            "sku_code",
            "name",
            "jan_code",
            "on_hand_qty",
            "status",
            "best_seller",
            "updated_at",
            "stock_managed",
            "archived",
            "low_stock_threshold",
            "velocity_per_day",
        ]
    )
    for r in rows:
        writer.writerow(
            [
                r["sku_code"],
                r["name"],
                r["jan_code"] or "",
                r["on_hand_qty"],
                STATUS_LABELS[classify(r["on_hand_qty"], r["low_stock_threshold"])],
                "はい" if r["id"] in best_sellers else "",
                r["updated_at"].strftime("%Y-%m-%d %H:%M:%S") if r["updated_at"] else "",
                "対象" if r["is_stock_managed"] else "対象外",
                "はい" if r["archived_at"] else "",
                r["low_stock_threshold"],
                f"{r['velocity_per_day']:.2f}" if r["velocity_per_day"] is not None else "",
            ]
        )
    return csv_response(buf.getvalue(), filename="inventory.csv")


# --------------------------------------------------------------------------
# Lifecycle toggles
#
# The bulk work is done by the two one-shot CLIs (mark_non_inventory,
# archive_legacy_skus). These row-level actions exist for the long tail the
# client discovers afterwards — a coupon SKU that turns up in month three —
# so a new one does not require a developer and a proxy session.
# --------------------------------------------------------------------------


def _back_to_list(request: Request, flash: str) -> RedirectResponse:
    """Return to the list the operator was looking at, filters intact."""
    params = {k: v for k, v in request.query_params.items() if k != "flash"}
    params["flash"] = flash
    return RedirectResponse(url=f"/admin/inventory?{urlencode(params)}", status_code=303)


@router.post("/inventory/{master_sku_id}/stock-managed")
async def toggle_stock_managed(
    request: Request,
    master_sku_id: int,
    operator: OperatorDep,
    session: AsyncSession = Depends(get_session),
    kind: str = Form("other"),
) -> Response:
    """Flip 在庫管理対象 / 対象外 for one master.

    Turning management OFF does NOT zero an accumulated negative — that is a
    stock correction with its own audit trail, and doing it silently inside a
    visibility toggle would hide a real inventory event. The operator is told to
    use 手動調整 (or the CLI for a batch).
    """
    master = await session.get(MasterSku, master_sku_id)
    if master is None:
        return _back_to_list(request, "notfound")

    turning_off = master.is_stock_managed
    # The CHECK constraint requires the flag and the kind to move together.
    master.is_stock_managed = not turning_off
    master.non_inventory_kind = (kind.strip() or "other") if turning_off else None
    await session.commit()
    log.info(
        "inventory.stock_managed.toggled",
        master_sku_id=master_sku_id,
        sku_code=master.sku_code,
        is_stock_managed=master.is_stock_managed,
        operator=operator,
    )
    return _back_to_list(request, "unmanaged" if turning_off else "managed")


@router.post("/inventory/{master_sku_id}/archive")
async def toggle_archived(
    request: Request,
    master_sku_id: int,
    operator: OperatorDep,
    session: AsyncSession = Depends(get_session),
    reason: str = Form(""),
) -> Response:
    master = await session.get(MasterSku, master_sku_id)
    if master is None:
        return _back_to_list(request, "notfound")

    if master.archived_at is not None:
        master.archived_at = None
        master.archived_reason = None
        await session.commit()
        log.info(
            "inventory.archive.cleared",
            master_sku_id=master_sku_id,
            sku_code=master.sku_code,
            operator=operator,
        )
        return _back_to_list(request, "unarchived")

    reasons = (await archive_blockers(session, [master.id])).reasons_for(master.id)
    if reasons:
        log.info(
            "inventory.archive.blocked",
            master_sku_id=master_sku_id,
            sku_code=master.sku_code,
            reasons=reasons,
        )
        return _back_to_list(request, "archive_blocked")

    master.archived_at = datetime.now(UTC)
    master.archived_reason = reason.strip() or f"手動アーカイブ ({operator})"
    await session.commit()
    log.info(
        "inventory.archive.set",
        master_sku_id=master_sku_id,
        sku_code=master.sku_code,
        operator=operator,
    )
    return _back_to_list(request, "archived")


def _flash(token: str | None) -> dict[str, str] | None:
    table = {
        "managed": ("ok", "在庫管理対象に戻しました。以後の受注で在庫を消費します。"),
        "unmanaged": (
            "ok",
            "在庫管理対象外に設定しました。以後の受注では在庫を消費しません。"
            "既に発生しているマイナス在庫は手動調整で補正してください。",
        ),
        "archived": ("ok", "アーカイブしました。通常の一覧には表示されなくなります。"),
        "unarchived": ("ok", "アーカイブを解除しました。"),
        "archive_blocked": (
            "error",
            "このSKUはまだ使用中のためアーカイブできません "
            "(在庫あり / 有効なマッピングあり / 有効なセットの構成品)。",
        ),
        "notfound": ("error", "指定したマスターSKUが見つかりません。"),
    }
    if not token or token not in table:
        return None
    kind, message = table[token]
    return {"kind": kind, "message": message}
