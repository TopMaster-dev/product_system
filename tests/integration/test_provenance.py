"""The dashboard finds the rollup run the rollup actually recorded.

The writer recorded "success" and the dashboard looked for "succeeded", so it
never found one: 「最終集計」 was never shown and the stale-data warning could
never fire — a check that looks healthy precisely because it cannot fail. Both
sides are exercised for real here, writer through the recorder the scheduled
job uses, reader through the query the overview runs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from app.cli.rebuild_daily_metrics import RebuildOutcome, _record_run
from app.services.analytics_query import provenance

pytestmark = pytest.mark.integration


async def test_a_recorded_successful_run_is_what_the_dashboard_reports(
    _test_engine: AsyncEngine,
) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False)
    started = datetime.now(UTC) - timedelta(minutes=5)
    await _record_run(
        factory, job_name="hourly", started_at=started, outcome=RebuildOutcome(), triggered_by=None
    )

    async with factory() as session:
        source = await provenance(session, now=datetime.now(UTC))

    assert source.last_rollup_at is not None
    assert source.stale_hours is not None and source.stale_hours < 1
    assert source.is_stale is False


async def test_a_failed_run_does_not_count_as_fresh(_test_engine: AsyncEngine) -> None:
    factory = async_sessionmaker(_test_engine, expire_on_commit=False)
    await _record_run(
        factory,
        job_name="hourly",
        started_at=datetime.now(UTC),
        outcome=RebuildOutcome(error="boom"),
        triggered_by=None,
    )

    async with factory() as session:
        source = await provenance(session, now=datetime.now(UTC))

    assert source.last_rollup_at is None


async def test_an_old_success_raises_the_stale_warning(_test_engine: AsyncEngine) -> None:
    """The warning the client relies on to know the figures stopped updating."""
    factory = async_sessionmaker(_test_engine, expire_on_commit=False)
    await _record_run(
        factory,
        job_name="hourly",
        started_at=datetime.now(UTC) - timedelta(hours=6),
        outcome=RebuildOutcome(),
        triggered_by=None,
    )

    async with factory() as session:
        source = await provenance(session, now=datetime.now(UTC) + timedelta(hours=5))

    assert source.is_stale is True
