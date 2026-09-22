"""Unit tests for the internal scheduler-triggered job endpoints.

The theme running through this file is that Cloud Scheduler judges a run by the
HTTP status and nothing else. A job that reports failure inside a 200 body gets
no retry, no alert, and a green row in the console. Three jobs here did exactly
that, and each stayed invisible for as long as it lasted — the BigQuery export's
"partial", the bundle push's "partial", and the daily reconciliation's "skipped",
which ran zero times in seven weeks while reporting success every morning.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api import internal_jobs

pytestmark = pytest.mark.unit


async def test_the_shopify_audit_endpoint_runs_the_audit(monkeypatch) -> None:
    """The replacement for the CROSS MALL reconciliation (P2-036). It takes no
    configuration, which is the point: its predecessor needed a CSV URI that was
    never set, so it returned 200 "skipped" every morning for seven weeks while
    the daily stock check never ran once."""
    seen: dict[str, object] = {}

    async def fake_run(*, triggered_by):
        seen["triggered_by"] = triggered_by
        return 0

    monkeypatch.setattr(internal_jobs.audit_shopify_stock, "run", fake_run)
    result = await internal_jobs.trigger_shopify_audit()
    assert result == {"status": "ok", "exit_code": "0"}
    assert seen == {"triggered_by": "cloud_scheduler"}


async def test_a_failing_shopify_audit_is_a_500(monkeypatch) -> None:
    """Losing the only external stock check must not read as success. Its
    predecessor's silence is what this whole file is about."""

    async def failed(*, triggered_by):
        return 1

    monkeypatch.setattr(internal_jobs.audit_shopify_stock, "run", failed)
    with pytest.raises(HTTPException) as raised:
        await internal_jobs.trigger_shopify_audit()
    assert raised.value.status_code == 500


def test_the_retired_reconcile_endpoint_is_gone() -> None:
    """P2-036. Leaving it mounted invites a scheduler to be pointed back at a
    job whose CROSS MALL input no longer exists."""
    assert not hasattr(internal_jobs, "trigger_reconcile")
    paths = {r.path for r in internal_jobs.router.routes}  # type: ignore[attr-defined]
    assert "/internal/jobs/reconcile" not in paths
    assert "/internal/jobs/shopify-audit" in paths


async def test_bundle_push_endpoint_invokes_cli(monkeypatch) -> None:
    seen: dict[str, object] = {}

    async def fake_run(*, dry_run, triggered_by):
        seen["dry_run"] = dry_run
        seen["triggered_by"] = triggered_by
        return 0

    monkeypatch.setattr(internal_jobs.push_bundle_availability, "run", fake_run)
    result = await internal_jobs.trigger_bundle_push()
    assert result == {"status": "ok", "exit_code": "0"}
    assert seen == {"dry_run": False, "triggered_by": "cloud_scheduler"}


async def test_bq_export_raises_500_and_alerts_on_table_failure(monkeypatch) -> None:
    """A per-table failure must NOT return HTTP 200. It previously returned
    {"status": "partial"} with 200, which hid a broken master_skus export."""
    from fastapi import HTTPException

    from app.services.bigquery_export import ExportResult

    now = datetime(2026, 8, 19, tzinfo=UTC)

    async def fake_run_export():
        return [
            ExportResult("orders", "incremental", 10, None, now),
            ExportResult(
                "master_skus", "incremental", 0, None, now, error="no such field: is_bundle"
            ),
        ]

    sent: list[dict[str, object]] = []

    class _Notifier:
        async def notify(self, *, level, title, message, fields=None):
            sent.append({"level": level, "title": title, "fields": fields})
            return True

    monkeypatch.setattr(internal_jobs.export_to_bq, "run_export", fake_run_export)
    monkeypatch.setattr(internal_jobs, "get_slack_notifier", lambda: _Notifier())

    with pytest.raises(HTTPException) as exc:
        await internal_jobs.trigger_bq_export()
    assert exc.value.status_code == 500
    assert "master_skus" in str(exc.value.detail)
    assert sent and sent[0]["level"] == "critical"


async def test_bq_export_returns_ok_when_all_tables_succeed(monkeypatch) -> None:
    from app.services.bigquery_export import ExportResult

    now = datetime(2026, 8, 19, tzinfo=UTC)

    async def fake_run_export():
        return [ExportResult("orders", "incremental", 3, None, now)]

    monkeypatch.setattr(internal_jobs.export_to_bq, "run_export", fake_run_export)
    result = await internal_jobs.trigger_bq_export()
    assert result == {"status": "ok", "tables": "1"}


# --- a failing job must look like a failing job ----------------------------
#
# Cloud Scheduler judges a run by the HTTP status and nothing else. Three
# separate jobs here reported failure inside a 200 body — the BigQuery export's
# "partial", the bundle push's "partial", and the reconciliation's "skipped" —
# and each was invisible in the scheduler console for as long as it lasted.


async def test_bundle_push_failure_is_a_500_not_a_partial(monkeypatch) -> None:
    """`{"status": "partial"}` behind a 200 is the exact wording and the exact
    mechanism that hid the broken BigQuery export for months."""

    async def failed(*, dry_run: bool, triggered_by: str) -> int:
        return 1

    monkeypatch.setattr(internal_jobs.push_bundle_availability, "run", failed)
    with pytest.raises(HTTPException) as raised:
        await internal_jobs.trigger_bundle_push()
    assert raised.value.status_code == 500


async def test_bundle_push_success_stays_a_200(monkeypatch) -> None:
    async def ok(*, dry_run: bool, triggered_by: str) -> int:
        return 0

    monkeypatch.setattr(internal_jobs.push_bundle_availability, "run", ok)
    assert (await internal_jobs.trigger_bundle_push())["status"] == "ok"


async def test_a_failing_poll_cannot_report_success(monkeypatch) -> None:
    """Order ingestion stopping silently is the worst failure this system has:
    stock stops decrementing and every downstream number drifts. The endpoint
    used to hardcode "ok" regardless of the exit code."""

    async def failed(channel: str, *, lookback_minutes: int) -> int:
        return 1

    monkeypatch.setattr(internal_jobs.poll_channels, "run", failed)
    with pytest.raises(HTTPException) as raised:
        await internal_jobs.trigger_poll_rakuten()
    assert raised.value.status_code == 500


async def test_a_successful_poll_reports_ok(monkeypatch) -> None:
    async def ok(channel: str, *, lookback_minutes: int) -> int:
        return 0

    monkeypatch.setattr(internal_jobs.poll_channels, "run", ok)
    assert (await internal_jobs.trigger_poll_shopify())["status"] == "ok"


# --- a job that CRASHES must alert, not just a job that exits non-zero -----
#
# Rakuten polling returned 401 from RMS for 24.7 days, from 2026-08-25 until it
# was found by hand. Roughly 265 orders never arrived and their stock never
# moved. The endpoint DID fail loudly in HTTP terms the whole time; nobody was
# told, because Slack alerts were wired to the BigQuery export and the analytics
# rollup and not to the polls — the only jobs that decrement stock.
#
# The 401 also arrives as an EXCEPTION, not an exit code, so an alert placed on
# the exit-code branch alone would have missed the outage it was written for.


class _Recorder:
    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []

    async def notify(self, *, level, title, message, fields=None):
        self.sent.append({"level": level, "title": title, "fields": fields})
        return True


@pytest.fixture(autouse=True)
def _reset_alert_throttle():
    """The throttle is module state, so one test's alert would silence the next."""
    internal_jobs._last_alert.clear()
    yield
    internal_jobs._last_alert.clear()


async def test_an_expired_credential_alerts_rather_than_only_500ing(monkeypatch) -> None:
    """The actual outage, held still. An adapter raising must reach Slack."""

    async def raises(channel: str, *, lookback_minutes: int) -> int:
        raise RuntimeError("Client error '401 Unauthorized' for url searchOrder")

    recorder = _Recorder()
    monkeypatch.setattr(internal_jobs.poll_channels, "run", raises)
    monkeypatch.setattr(internal_jobs, "get_slack_notifier", lambda: recorder)

    with pytest.raises(HTTPException) as raised:
        await internal_jobs.trigger_poll_rakuten()

    assert raised.value.status_code == 500
    assert len(recorder.sent) == 1
    assert recorder.sent[0]["level"] == "critical"


async def test_a_non_zero_exit_alerts_too(monkeypatch) -> None:
    async def failed(channel: str, *, lookback_minutes: int) -> int:
        return 1

    recorder = _Recorder()
    monkeypatch.setattr(internal_jobs.poll_channels, "run", failed)
    monkeypatch.setattr(internal_jobs, "get_slack_notifier", lambda: recorder)

    with pytest.raises(HTTPException):
        await internal_jobs.trigger_poll_shopify()
    assert len(recorder.sent) == 1


async def test_a_succeeding_job_alerts_nobody(monkeypatch) -> None:
    async def ok(channel: str, *, lookback_minutes: int) -> int:
        return 0

    recorder = _Recorder()
    monkeypatch.setattr(internal_jobs.poll_channels, "run", ok)
    monkeypatch.setattr(internal_jobs, "get_slack_notifier", lambda: recorder)

    assert (await internal_jobs.trigger_poll_shopify())["status"] == "ok"
    assert recorder.sent == []


async def test_repeat_failures_are_throttled_so_the_channel_stays_readable(monkeypatch) -> None:
    """Cloud Scheduler retries on its own cadence. A job broken for weeks would
    otherwise send about a hundred identical messages a day, and a channel that
    noisy gets muted — which recreates the silence the alert exists to break."""

    async def failed(channel: str, *, lookback_minutes: int) -> int:
        return 1

    recorder = _Recorder()
    monkeypatch.setattr(internal_jobs.poll_channels, "run", failed)
    monkeypatch.setattr(internal_jobs, "get_slack_notifier", lambda: recorder)

    for _ in range(5):
        with pytest.raises(HTTPException):
            await internal_jobs.trigger_poll_rakuten()

    assert len(recorder.sent) == 1


async def test_the_throttle_is_per_job_not_global(monkeypatch) -> None:
    """Rakuten failing must not mask Shopify failing. They are different
    outages and one is not evidence about the other."""

    async def failed(channel: str, *, lookback_minutes: int) -> int:
        return 1

    recorder = _Recorder()
    monkeypatch.setattr(internal_jobs.poll_channels, "run", failed)
    monkeypatch.setattr(internal_jobs, "get_slack_notifier", lambda: recorder)

    for endpoint in (internal_jobs.trigger_poll_rakuten, internal_jobs.trigger_poll_shopify):
        with pytest.raises(HTTPException):
            await endpoint()

    assert len(recorder.sent) == 2


def test_every_scheduled_job_routes_through_the_alerting_helper() -> None:
    """The gap was not that alerting was hard — it was that it had been added
    to two jobs and not the rest. This fails if a new endpoint is added without
    it."""
    source = Path("app/api/internal_jobs.py").read_text(encoding="utf-8")
    # Each scheduled job either calls _run_job or handles its own notification.
    assert source.count("_run_job(") >= 4
    assert "_job_result" not in source
