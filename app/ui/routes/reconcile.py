"""Reconcile admin UI (Phase 1-B F3.2) + CROSS MALL 在庫CSV 取込.

Two connected surfaces:

* **在庫CSV取込** — upload the CROSS MALL stock CSV, get an
  *upload → 検証 → 確認 → 実行* flow: the file is validated (encoding,
  required columns, per-row issues), a diff preview is shown WITHOUT
  touching the DB, and only on explicit confirmation is a ReconcileRun
  created. The raw CSV rides the confirm step as a base64 hidden field so
  the flow is stateless across Cloud Run instances.

* **リコンサイル** — list runs, review each run's diffs, approve/skip
  individual diffs, and finalize or cancel the run. Approving a diff writes
  a stocktake event + updates the snapshot (via ReconcileService); channel
  pushes stay batched per D-6 (the scheduled push job propagates).
"""

from __future__ import annotations

import base64
import csv
import io
import tempfile
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response, StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import __version__
from app.cli.reconcile_inventory import (
    COL_PRODUCT_CODE,
    COL_QTY,
    ENC,
    aggregate_csv_variants,
    collect_diffs,
)
from app.db import get_session
from app.models import (
    MasterSku,
    ReconcileDiff,
    ReconcileRun,
    ReconcileRunStatusEnum,
)
from app.services.reconcile import ReconcileService
from app.ui.auth import OperatorDep
from app.ui.deps import templates

router = APIRouter(prefix="/reconcile")

REQUIRED_COLUMNS = (COL_PRODUCT_CODE, COL_QTY)
MAX_ROW_ISSUES = 50


# ---------------------------------------------------------------------------
# CSV inspection (pure) — encoding, required columns, per-row issues.
# ---------------------------------------------------------------------------


def inspect_csv(data: bytes) -> dict[str, Any]:
    """Validate a CROSS MALL stock CSV without touching the DB. Returns
    `fatal` (blocking errors), `row_issues` (skippable rows w/ line numbers),
    and row counts for the preview."""
    result: dict[str, Any] = {"fatal": [], "row_issues": [], "total_rows": 0, "valid_rows": 0}
    if not data:
        result["fatal"].append("ファイルが空です。")
        return result
    try:
        text = data.decode(ENC)
    except UnicodeDecodeError:
        result["fatal"].append(
            "文字コードが Shift-JIS(CP932) ではありません。"
            "CROSS MALL の元ファイルをそのままアップロードしてください。"
        )
        return result
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        result["fatal"].append("ヘッダー行がありません。")
        return result
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        result["fatal"].append("必須列がありません: " + " / ".join(missing))
        return result

    code_idx = header.index(COL_PRODUCT_CODE)
    qty_idx = header.index(COL_QTY)
    for lineno, row in enumerate(reader, start=2):
        if not row or len(row) <= max(code_idx, qty_idx):
            continue
        result["total_rows"] += 1
        code = row[code_idx].strip()
        raw = row[qty_idx].strip()
        if not code:
            if len(result["row_issues"]) < MAX_ROW_ISSUES:
                result["row_issues"].append({"line": lineno, "reason": "商品コードが空です"})
            continue
        if not raw:
            continue
        try:
            int(raw)
        except ValueError:
            if len(result["row_issues"]) < MAX_ROW_ISSUES:
                result["row_issues"].append(
                    {"line": lineno, "reason": f"在庫数量が数値ではありません: '{raw}'"}
                )
            continue
        result["valid_rows"] += 1
    return result


def _aggregate_uploaded_csv(data: bytes) -> dict[str, int]:
    """Persist uploaded bytes to a temp file and aggregate them. Kept synchronous
    (and out of the async handlers) so the brief blocking file IO is a single
    contained step, mirroring how the reconcile CLI reads its CSV."""
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".csv")  # noqa: SIM115
    try:
        tmp.write(data)
        tmp.close()
        return aggregate_csv_variants(Path(tmp.name))
    finally:
        Path(tmp.name).unlink(missing_ok=True)


async def _diff_preview(
    data: bytes, session: AsyncSession
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Aggregate the CSV and compute the proposed diffs (read-only), joined to
    master SKU codes, sorted by magnitude of change."""
    aggregates = _aggregate_uploaded_csv(data)
    diffs, summary = await collect_diffs(aggregates, session)
    view: list[dict[str, Any]] = []
    if diffs:
        ids = [d.master_sku_id for d in diffs]
        rows = (
            await session.execute(
                select(MasterSku.id, MasterSku.sku_code, MasterSku.name).where(
                    MasterSku.id.in_(ids)
                )
            )
        ).all()
        info = {mid: (code, name) for mid, code, name in rows}
        for d in diffs:
            code, name = info.get(d.master_sku_id, ("?", ""))
            view.append(
                {
                    "sku_code": code,
                    "name": name,
                    "current": d.current_qty,
                    "target": d.target_qty,
                    "delta": d.target_qty - d.current_qty,
                }
            )
        view.sort(key=lambda r: abs(r["delta"]), reverse=True)
    return view, summary


# ---------------------------------------------------------------------------
# 在庫CSV取込  (upload → preview → execute)
# ---------------------------------------------------------------------------


@router.get("/upload")
async def upload_form(request: Request, operator: OperatorDep) -> Response:
    return templates.TemplateResponse(
        request,
        "reconcile_upload.html",
        {
            "operator": operator,
            "version": __version__,
            "flash": _flash(request.query_params.get("flash")),
        },
    )


@router.post("/upload")
async def upload_preview(
    request: Request,
    operator: OperatorDep,
    file: Annotated[UploadFile, File()],
    session: AsyncSession = Depends(get_session),
) -> Response:
    data = await file.read()
    inspection = inspect_csv(data)
    diffs: list[dict[str, Any]] = []
    summary: dict[str, int] = {}
    if not inspection["fatal"]:
        diffs, summary = await _diff_preview(data, session)
    return templates.TemplateResponse(
        request,
        "reconcile_preview.html",
        {
            "operator": operator,
            "version": __version__,
            "filename": file.filename or "upload.csv",
            "inspection": inspection,
            "diffs": diffs,
            "summary": summary,
            "csv_b64": base64.b64encode(data).decode("ascii") if not inspection["fatal"] else "",
        },
    )


@router.post("/execute")
async def upload_execute(
    operator: OperatorDep,
    csv_b64: Annotated[str, Form()],
    filename: Annotated[str, Form()] = "upload.csv",
    session: AsyncSession = Depends(get_session),
) -> Response:
    try:
        data = base64.b64decode(csv_b64)
    except (ValueError, TypeError):
        return RedirectResponse(url="/admin/reconcile/upload?flash=badcsv", status_code=303)
    if inspect_csv(data)["fatal"]:
        return RedirectResponse(url="/admin/reconcile/upload?flash=badcsv", status_code=303)

    aggregates = _aggregate_uploaded_csv(data)
    async with session.begin():
        diffs, _ = await collect_diffs(aggregates, session)
        run = await ReconcileService(session).start_run(
            source="admin_upload",
            triggered_by=operator,
            diffs=diffs,
            csv_filename=filename,
        )
        run_id = run.id
    return RedirectResponse(url=f"/admin/reconcile/{run_id}?flash=created", status_code=303)


# ---------------------------------------------------------------------------
# リコンサイル  (run list + diff review)
# ---------------------------------------------------------------------------


@router.get("")
async def reconcile_list(
    request: Request,
    operator: OperatorDep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    runs = (
        (
            await session.execute(
                select(ReconcileRun).order_by(ReconcileRun.started_at.desc()).limit(100)
            )
        )
        .scalars()
        .all()
    )
    pending_runs = await session.scalar(
        select(func.count())
        .select_from(ReconcileRun)
        .where(ReconcileRun.status == ReconcileRunStatusEnum.PENDING_APPROVAL.value)
    )
    return templates.TemplateResponse(
        request,
        "reconcile_list.html",
        {
            "operator": operator,
            "version": __version__,
            "runs": runs,
            "pending_runs": pending_runs or 0,
            "flash": _flash(request.query_params.get("flash")),
        },
    )


@router.get("/{run_id}")
async def reconcile_detail(
    request: Request,
    run_id: int,
    operator: OperatorDep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    run = await session.get(ReconcileRun, run_id)
    if run is None:
        return RedirectResponse(url="/admin/reconcile?flash=notfound", status_code=303)
    diff_rows = (
        (
            await session.execute(
                select(
                    ReconcileDiff.id,
                    ReconcileDiff.master_sku_id,
                    ReconcileDiff.current_qty,
                    ReconcileDiff.target_qty,
                    ReconcileDiff.delta,
                    ReconcileDiff.decision,
                    ReconcileDiff.decided_by,
                    MasterSku.sku_code,
                    MasterSku.name,
                )
                .join(MasterSku, MasterSku.id == ReconcileDiff.master_sku_id)
                .where(ReconcileDiff.reconcile_run_id == run_id)
                .order_by(ReconcileDiff.decision, func.abs(ReconcileDiff.delta).desc())
            )
        )
        .mappings()
        .all()
    )

    counts = {"pending": 0, "approved": 0, "skipped": 0}
    for d in diff_rows:
        counts[d["decision"]] = counts.get(d["decision"], 0) + 1

    return templates.TemplateResponse(
        request,
        "reconcile_detail.html",
        {
            "operator": operator,
            "version": __version__,
            "run": run,
            "diffs": diff_rows,
            "counts": counts,
            "flash": _flash(request.query_params.get("flash")),
        },
    )


@router.post("/{run_id}/diffs/{diff_id}/approve")
async def diff_approve(
    run_id: int,
    diff_id: int,
    operator: OperatorDep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    return await _diff_action(session, run_id, diff_id, operator, approve=True)


@router.post("/{run_id}/diffs/{diff_id}/skip")
async def diff_skip(
    run_id: int,
    diff_id: int,
    operator: OperatorDep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    return await _diff_action(session, run_id, diff_id, operator, approve=False)


async def _diff_action(
    session: AsyncSession, run_id: int, diff_id: int, operator: str, *, approve: bool
) -> Response:
    try:
        async with session.begin():
            svc = ReconcileService(session)
            if approve:
                await svc.approve_diff(diff_id=diff_id, approved_by=operator)
            else:
                await svc.skip_diff(diff_id=diff_id, approved_by=operator)
    except (RuntimeError, ValueError):
        return RedirectResponse(url=f"/admin/reconcile/{run_id}?flash=difffailed", status_code=303)
    flash = "approved" if approve else "skipped"
    return RedirectResponse(url=f"/admin/reconcile/{run_id}?flash={flash}", status_code=303)


@router.post("/{run_id}/finalize")
async def reconcile_finalize(
    run_id: int,
    operator: OperatorDep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    try:
        async with session.begin():
            await ReconcileService(session).finalize_run(run_id=run_id, approved_by=operator)
    except (RuntimeError, ValueError):
        return RedirectResponse(
            url=f"/admin/reconcile/{run_id}?flash=finalizefailed", status_code=303
        )
    return RedirectResponse(url=f"/admin/reconcile/{run_id}?flash=finalized", status_code=303)


@router.post("/{run_id}/cancel")
async def reconcile_cancel(
    run_id: int,
    operator: OperatorDep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    try:
        async with session.begin():
            await ReconcileService(session).cancel_run(
                run_id=run_id, cancelled_by=operator, reason="admin UI cancel"
            )
    except (RuntimeError, ValueError):
        return RedirectResponse(
            url=f"/admin/reconcile/{run_id}?flash=cancelfailed", status_code=303
        )
    return RedirectResponse(url=f"/admin/reconcile/{run_id}?flash=cancelled", status_code=303)


@router.get("/{run_id}/export.csv")
async def reconcile_export(
    run_id: int,
    operator: OperatorDep,
    session: AsyncSession = Depends(get_session),
) -> Response:
    rows = (
        await session.execute(
            select(
                MasterSku.sku_code,
                MasterSku.name,
                ReconcileDiff.current_qty,
                ReconcileDiff.target_qty,
                ReconcileDiff.delta,
                ReconcileDiff.decision,
                ReconcileDiff.decided_by,
            )
            .join(MasterSku, MasterSku.id == ReconcileDiff.master_sku_id)
            .where(ReconcileDiff.reconcile_run_id == run_id)
            .order_by(MasterSku.sku_code)
        )
    ).all()
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        ["sku_code", "name", "current_qty", "target_qty", "delta", "decision", "decided_by"]
    )
    for r in rows:
        writer.writerow([r[0], r[1], r[2], r[3], r[4], r[5], r[6] or ""])
    buf.seek(0)
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="reconcile_run_{run_id}.csv"'},
    )


def _flash(token: str | None) -> dict[str, str] | None:
    table = {
        "created": ("ok", "取込が完了しました。差分を確認し、承認・確定してください。"),
        "approved": ("ok", "差分を承認し、在庫へ反映しました。"),
        "skipped": ("ok", "差分をスキップしました。"),
        "finalized": ("ok", "確定しました。チャネルへの反映は定期同期で行われます。"),
        "cancelled": ("ok", "この照合を取り消しました。"),
        "notfound": ("error", "対象の照合が見つかりません。"),
        "badcsv": ("error", "CSVを検証できませんでした。もう一度アップロードしてください。"),
        "difffailed": ("error", "この差分は処理できませんでした(状態を確認してください)。"),
        "finalizefailed": ("error", "未処理の差分が残っているため確定できません。"),
        "cancelfailed": ("error", "承認済みの差分があるため取り消せません。"),
    }
    if not token or token not in table:
        return None
    kind, message = table[token]
    return {"kind": kind, "message": message}
