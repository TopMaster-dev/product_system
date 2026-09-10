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

import pytest
from fastapi import HTTPException

from app.api import internal_jobs
from app.config import Settings

pytestmark = pytest.mark.unit


async def test_reconcile_fails_loudly_when_uri_unset(monkeypatch) -> None:
    """This used to return 200 "skipped", and that is how the daily stock
    reconciliation never ran for seven weeks while Cloud Scheduler recorded a
    success every morning. `reconcile_csv_uri` was never set in Terraform, so
    the baseline seeded on 2026-07-20 was never corrected — and it stayed at
    roughly six times what both sales channels held.

    A scheduler calling an endpoint that is not configured to do anything is a
    misconfiguration, not a no-op.
    """
    monkeypatch.setattr(internal_jobs, "get_settings", lambda: Settings(reconcile_csv_uri=""))
    with pytest.raises(HTTPException) as raised:
        await internal_jobs.trigger_reconcile()
    assert raised.value.status_code == 500


async def test_reconcile_runs_when_uri_configured(monkeypatch) -> None:
    monkeypatch.setattr(
        internal_jobs, "get_settings", lambda: Settings(reconcile_csv_uri="gs://bucket/stock.csv")
    )
    seen: dict[str, object] = {}

    async def fake_run(csv_path, *, triggered_by):
        seen["csv"] = csv_path
        seen["triggered_by"] = triggered_by
        return 0

    monkeypatch.setattr(internal_jobs.reconcile_inventory, "run", fake_run)
    result = await internal_jobs.trigger_reconcile()
    assert result == {"status": "ok", "exit_code": "0"}
    assert seen == {"csv": "gs://bucket/stock.csv", "triggered_by": "cloud_scheduler"}


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
