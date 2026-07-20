"""Unit tests for the internal scheduler-triggered job endpoints.

Focus on the Phase 1-B additions: the daily reconcile trigger (no-op when
unconfigured, runs when a CSV URI is set) and the batched bundle push.
"""

from __future__ import annotations

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
