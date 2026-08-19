"""Unit tests for the internal scheduler-triggered job endpoints.

Focus on the Phase 1-B additions: the daily reconcile trigger (no-op when
unconfigured, runs when a CSV URI is set) and the batched bundle push.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.api import internal_jobs
from app.config import Settings

pytestmark = pytest.mark.unit


async def test_reconcile_skips_when_uri_unset(monkeypatch) -> None:
    monkeypatch.setattr(internal_jobs, "get_settings", lambda: Settings(reconcile_csv_uri=""))
    result = await internal_jobs.trigger_reconcile()
    assert result["status"] == "skipped"


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
