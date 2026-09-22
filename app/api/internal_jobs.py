"""Internal endpoints invoked by Cloud Scheduler / Cloud Tasks via
OIDC-authenticated POST.

These are NOT for public/admin use — they wrap the same logic exposed
in `app/cli/` (scheduler) or dispatch a registered task handler (Cloud Tasks).
"""

from __future__ import annotations

from collections.abc import Awaitable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException

from app.api.auth_internal import require_internal_caller
from app.cli import (
    audit_shopify_stock,
    export_to_bq,
    poll_channels,
    push_bundle_availability,
    rebuild_daily_metrics,
)
from app.logging import get_logger
from app.notifications.slack import get_slack_notifier
from app.services.handlers import dispatch

log = get_logger(__name__)

# The dependency guards EVERY route on this router, including any added later.
# Listing endpoints individually is how one gets forgotten, and the one most
# worth forgetting is `tasks/run`, which dispatches a caller-supplied payload.
router = APIRouter(
    prefix="/internal/jobs",
    tags=["internal"],
    dependencies=[Depends(require_internal_caller)],
)


#: How long a job stays quiet after alerting, per instance. Cloud Scheduler
#: retries on its own cadence, so a job broken for weeks would otherwise send
#: roughly a hundred identical messages a day — and a channel that noisy gets
#: muted, which recreates the silence the alert existed to break.
#:
#: Per INSTANCE, deliberately: Cloud Run may hold several, so this is a damper
#: rather than a guarantee. It degrades toward MORE alerts, never fewer, which
#: is the right direction for the only signal that order ingestion has stopped.
ALERT_QUIET_MINUTES = 60

_last_alert: dict[str, datetime] = {}


async def _alert_job_failure(job: str, detail: str) -> None:
    """Tell somebody. The reason this exists, in one paragraph:

    Rakuten order polling returned 401 from RMS for 24.7 days — from 2026-08-25
    until it was found by hand on 2026-09-19 — during which roughly 265 orders
    never reached the system and their stock never moved. The job WAS failing
    loudly in HTTP terms. Nothing was watching, because Slack alerts had been
    wired to the BigQuery export and the analytics rollup and not to the polls,
    which are the only thing that decrements stock.
    """
    now = datetime.now(UTC)
    last = _last_alert.get(job)
    if last is not None and now - last < timedelta(minutes=ALERT_QUIET_MINUTES):
        log.info("internal.alert_suppressed", job=job)
        return
    _last_alert[job] = now
    await get_slack_notifier().notify(
        level="critical",
        title=f"定期ジョブ {job} が失敗しています",
        message=(
            f"{job} が失敗しました。受注取込の場合、停止している間の販売は"
            "在庫から引かれず、売上にも計上されません。"
            f"なお同一ジョブの通知は{ALERT_QUIET_MINUTES}分間抑制されます。"
        ),
        fields=[("detail", detail[:300])],
    )


async def _run_job(job: str, work: Awaitable[int]) -> dict[str, str]:
    """Run a scheduled job and make every way it can fail visible.

    Cloud Scheduler judges a run by the HTTP status and nothing else, so a
    failure reported inside a 200 gets no retry, no alert and a green row. This
    project has been bitten by that three times — the BigQuery export's
    "partial", the bundle push's "partial", and the daily reconciliation's
    "skipped" for seven weeks while it never ran.

    Both failure shapes route through here, because they are equally silent and
    the second is what actually happened:

    * a non-zero EXIT CODE, which the CLI returns for a handled failure;
    * an EXCEPTION, which is how an expired credential arrives. The Rakuten 401
      propagated straight past the exit-code check, so an alert placed there
      would have missed the outage it was written for.
    """
    try:
        exit_code = await work
    except Exception as exc:
        log.exception("internal.job_crashed", job=job)
        await _alert_job_failure(job, repr(exc))
        raise HTTPException(status_code=500, detail=f"{job} crashed: {exc}") from exc

    if exit_code == 0:
        return {"status": "ok", "exit_code": "0"}
    log.error("internal.job_failed", job=job, exit_code=exit_code)
    await _alert_job_failure(job, f"exit code {exit_code}")
    raise HTTPException(status_code=500, detail=f"{job} failed: exit {exit_code}")


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


@router.post("/rollup-daily")
async def trigger_rollup_daily(mode: str = "incremental") -> dict[str, str]:
    """Rebuild the daily analytics rollups.

    Two schedulers hit this. `mode=incremental` (hourly) rebuilds only the JST
    days touched since the last success. `mode=repair` (nightly) rebuilds a
    trailing window unconditionally, covering anything the incremental window
    could have missed.

    A failure returns 500 so Cloud Scheduler retries and Slack fires — the same
    contract as the BigQuery export, and for the same reason: an aggregation job
    that reports success while producing nothing is how a broken pipeline hides
    for months.

    Being skipped because another run holds the lock is NOT a failure. The
    hourly and nightly jobs will eventually overlap, and that is the guard
    working.
    """
    repair = mode == "repair"
    outcome = await rebuild_daily_metrics.run(
        repair_days=rebuild_daily_metrics.DEFAULT_REPAIR_DAYS if repair else None,
        job_name="nightly-repair" if repair else "hourly",
        triggered_by="cloud_scheduler",
    )

    if outcome.error:
        log.error("internal.rollup.failed", mode=mode, error=outcome.error)
        await get_slack_notifier().notify(
            level="critical",
            title="分析ロールアップ失敗",
            message=(
                f"{mode} ロールアップが失敗しました。"
                " 分析画面の数値が更新されていない可能性があります。"
            ),
            fields=[("error", outcome.error[:300])],
        )
        raise HTTPException(status_code=500, detail=f"rollup failed: {outcome.error}")

    if outcome.skipped_locked:
        log.info("internal.rollup.skipped", mode=mode)
        return {"status": "skipped", "reason": "another rollup is running"}

    if outcome.remaining_days:
        # Succeeded but not caught up — its own state, never folded into "ok".
        log.warning("internal.rollup.behind", mode=mode, remaining_days=outcome.remaining_days)
        await get_slack_notifier().notify(
            level="error",
            title="分析ロールアップに未処理の日付があります",
            message=(
                f"{outcome.days_rebuilt}日分を再構築しましたが、"
                f"残り{outcome.remaining_days}日分が未処理です。"
                " `py -m app.cli.rebuild_daily_metrics --max-days 0` で追い付かせてください。"
            ),
            fields=[("remaining_days", str(outcome.remaining_days))],
        )
        return {
            "status": "behind",
            "days_rebuilt": str(outcome.days_rebuilt),
            "remaining_days": str(outcome.remaining_days),
        }

    log.info("internal.rollup.done", mode=mode, days_rebuilt=outcome.days_rebuilt)
    return {"status": "ok", "days_rebuilt": str(outcome.days_rebuilt)}


@router.post("/poll-shopify")
async def trigger_poll_shopify(lookback_minutes: int = 20) -> dict[str, str]:
    result = await _run_job(
        "poll-shopify", poll_channels.run("shopify", lookback_minutes=lookback_minutes)
    )
    log.info("internal.poll_shopify.done", exit_code=0, lookback_minutes=lookback_minutes)
    return result


@router.post("/poll-rakuten")
async def trigger_poll_rakuten(lookback_minutes: int = 10) -> dict[str, str]:
    result = await _run_job(
        "poll-rakuten", poll_channels.run("rakuten", lookback_minutes=lookback_minutes)
    )
    log.info("internal.poll_rakuten.done", exit_code=0, lookback_minutes=lookback_minutes)
    return result


@router.post("/shopify-audit")
async def trigger_shopify_audit() -> dict[str, str]:
    """Daily stock audit against Shopify (P2-035), the external check that
    replaces the CROSS MALL reconciliation (P2-036).

    Creates a ReconcileRun in pending_approval with run_type='shopify_audit';
    nothing reaches inventory until an operator approves the diffs in the admin
    UI, exactly as the CROSS MALL job worked (D-6).

    The job it replaces returned 200 "skipped" for seven weeks because its CSV
    URI was never configured, so nobody learned the daily check had never run.
    This one needs no configuration: it reads the shop it is already
    authenticated against, and a failure is a 500.
    """
    result = await _run_job(
        "shopify-audit", audit_shopify_stock.run(triggered_by="cloud_scheduler")
    )
    log.info("internal.shopify_audit.done", exit_code=0)
    return result


@router.post("/bundle-push")
async def trigger_bundle_push() -> dict[str, str]:
    """Batched bundle/shared-stock availability push to Shopify (D-6). Recomputes
    each parent's derived availability and pushes it; safe to run periodically."""
    # This said "partial" with HTTP 200 when pushes failed. "partial" behind a
    # 200 is the exact wording and the exact mechanism that hid the broken
    # BigQuery export for months.
    result = await _run_job(
        "bundle-push",
        push_bundle_availability.run(dry_run=False, triggered_by="cloud_scheduler"),
    )
    log.info("internal.bundle_push.done", exit_code=0)
    return result


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
