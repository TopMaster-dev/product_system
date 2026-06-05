"""Internal endpoints invoked by Cloud Scheduler / Cloud Tasks via
OIDC-authenticated POST.

These are NOT for public/admin use — they wrap the same logic exposed
in `app/cli/` (scheduler) or dispatch a registered task handler (Cloud Tasks).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException

from app.cli import export_to_bq, poll_channels
from app.logging import get_logger
from app.services.handlers import dispatch

log = get_logger(__name__)

router = APIRouter(prefix="/internal/jobs", tags=["internal"])


@router.post("/bq-export")
async def trigger_bq_export() -> dict[str, str]:
    code = await export_to_bq.run()
    log.info("internal.bq_export.done", exit_code=code)
    return {"status": "ok" if code == 0 else "partial", "exit_code": str(code)}


@router.post("/poll-shopify")
async def trigger_poll_shopify(lookback_minutes: int = 20) -> dict[str, str]:
    code = await poll_channels.run("shopify", lookback_minutes=lookback_minutes)
    log.info("internal.poll_shopify.done", exit_code=code, lookback_minutes=lookback_minutes)
    return {"status": "ok", "exit_code": str(code)}


@router.post("/poll-rakuten")
async def trigger_poll_rakuten(lookback_minutes: int = 10) -> dict[str, str]:
    code = await poll_channels.run("rakuten", lookback_minutes=lookback_minutes)
    log.info("internal.poll_rakuten.done", exit_code=code, lookback_minutes=lookback_minutes)
    return {"status": "ok", "exit_code": str(code)}


@router.post("/tasks/run")
async def run_task(body: dict[str, Any] = Body(...)) -> dict[str, str]:
    """Receive a Cloud Tasks delivery and dispatch its registered handler.

    Body shape mirrors what `CloudTasksTaskQueue.enqueue` posts:
        {"name": "process_shopify_webhook", "payload": {...}}
    """
    name = body.get("name")
    payload = body.get("payload") or {}
    if not name:
        raise HTTPException(status_code=400, detail="missing task name")
    try:
        await dispatch(name, payload)
    except KeyError as exc:
        log.warning("internal.tasks.no_handler", name=name)
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    log.info("internal.tasks.done", name=name)
    return {"status": "ok"}
