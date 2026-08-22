"""Internal endpoints invoked by Cloud Scheduler / Cloud Tasks via
OIDC-authenticated POST.

These are NOT for public/admin use — they wrap the same logic exposed
in `app/cli/` (scheduler) or dispatch a registered task handler (Cloud Tasks).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Body, HTTPException

from app.cli import export_to_bq, poll_channels, push_bundle_availability, reconcile_inventory
from app.config import get_settings
from app.logging import get_logger
from app.notifications.slack import get_slack_notifier
from app.services.handlers import dispatch

log = get_logger(__name__)

router = APIRouter(prefix="/internal/jobs", tags=["internal"])


@router.post("/bq-export")
async def trigger_bq_export() -> dict[str, str]:
    """Daily BigQuery export.

    A per-table failure MUST surface: the load runs with `autodetect=False`
    against Terraform-pinned schemas, so an ORM column the JSON lacks fails the
    load. Returning HTTP 200 here made that invisible to Cloud Scheduler — the
    `master_skus` export was broken in production from migration 0005 until it
    was found by the schema-parity test. Failures now return 500 (Scheduler
    records/retries the job) and fire a Slack critical.
    """
    results = await export_to_bq.run_export()
    failed = [r for r in results if r.error]
    behind = [r for r in results if r.remaining_windows]

    if not failed:
        if behind:
            # Succeeded, but not caught up. Reported as its own state: treating
            # "behind" as "ok" is precisely how a three-month outage stayed
            # invisible. Not a 500 — nothing is broken, the schedule just cannot
            # close a backlog this large on its own.
            detail = ", ".join(f"{r.table_name}: 残り{r.remaining_windows}日分" for r in behind)
            log.warning("internal.bq_export.behind", detail=detail)
            await get_slack_notifier().notify(
                level="error",
                title="BigQuery エクスポートに未処理の期間があります",
                message=(
                    "今回の実行は成功しましたが、未反映の期間が残っています。"
                    " `py -m app.cli.export_to_bq --max-windows 0` で追い付かせてください。"
                ),
                fields=[(r.table_name, f"残り {r.remaining_windows} 窓") for r in behind],
            )
            return {"status": "behind", "tables": str(len(results)), "detail": detail}
        log.info("internal.bq_export.done", tables=len(results))
        return {"status": "ok", "tables": str(len(results))}

    detail = "; ".join(f"{r.table_name}: {r.error}" for r in failed)
    log.error("internal.bq_export.failed", failed=len(failed), detail=detail)
    await get_slack_notifier().notify(
        level="critical",
        title="BigQuery エクスポート失敗",
        message=(
            f"{len(failed)}/{len(results)} テーブルのエクスポートに失敗しました。"
            " スキーマ不一致の場合は infra/terraform/bq_schemas/*.json を修正してください。"
        ),
        fields=[(r.table_name, (r.error or "")[:200]) for r in failed],
    )
    raise HTTPException(status_code=500, detail=f"bq-export failed: {detail}")


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


@router.post("/reconcile")
async def trigger_reconcile() -> dict[str, str]:
    """Daily CROSS MALL reconciliation. Reads the stock CSV at the configured
    `reconcile_csv_uri` (gs://…) and creates a ReconcileRun in pending_approval —
    nothing is applied to inventory until an operator approves the diffs in the
    admin UI (per D-6). No-ops (does not error) when the URI is unset."""
    uri = get_settings().reconcile_csv_uri
    if not uri:
        log.warning("internal.reconcile.no_csv_uri")
        return {"status": "skipped", "reason": "reconcile_csv_uri not configured"}
    code = await reconcile_inventory.run(uri, triggered_by="cloud_scheduler")
    log.info("internal.reconcile.done", exit_code=code)
    return {"status": "ok" if code == 0 else "error", "exit_code": str(code)}


@router.post("/bundle-push")
async def trigger_bundle_push() -> dict[str, str]:
    """Batched bundle/shared-stock availability push to Shopify (D-6). Recomputes
    each parent's derived availability and pushes it; safe to run periodically."""
    code = await push_bundle_availability.run(dry_run=False, triggered_by="cloud_scheduler")
    log.info("internal.bundle_push.done", exit_code=code)
    return {"status": "ok" if code == 0 else "partial", "exit_code": str(code)}


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
