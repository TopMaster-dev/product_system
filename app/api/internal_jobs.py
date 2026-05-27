"""Internal endpoints invoked by Cloud Scheduler via OIDC-authenticated POST.

These are NOT for public/admin use — they only wrap the same logic exposed
in `app/cli/` so the scheduler can trigger periodic jobs without spawning
separate Cloud Run Jobs.
"""

from __future__ import annotations

from fastapi import APIRouter

from app.cli import export_to_bq, poll_channels
from app.logging import get_logger

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
