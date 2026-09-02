"""Rebuild the daily analytics rollups.

    py -m app.cli.rebuild_daily_metrics                        # incremental
    py -m app.cli.rebuild_daily_metrics --from 2026-05-27      # backfill
    py -m app.cli.rebuild_daily_metrics --repair-days 35       # trailing repair
    py -m app.cli.rebuild_daily_metrics --prune-before 2025-09-03

Three shapes of run, all the same code path:

**Incremental** (no arguments, and what the hourly job does): rebuild only the
JST days touched since the last success. See `AnalyticsRollupService` for why
that set is derived from the data rather than a fixed lookback.

**Explicit range** (`--from` / `--to`): the backfill. A cold start has no
watermark, so it must be told what to build — the incremental path deliberately
returns nothing rather than deciding on its own to rebuild all of history.

**Repair** (`--repair-days`): rebuild a trailing window unconditionally, for the
nightly job. It exists because the incremental window trusts `created_at` and
`updated_at`, and a row written by something that bypasses the ORM would not
move either. This is the belt to that braces.

ONLY ONE RUN AT A TIME
----------------------
Guarded with `pg_try_advisory_lock`. The hourly and nightly jobs will eventually
overlap — a slow hourly run still going at 03:00 — and two rollups rebuilding
the same day concurrently interleave their DELETE and INSERT, leaving a day
with half of each. A second run exits 0 having done nothing, because "someone
else is already doing it" is success, not failure.

THE RUN RECORD IS COMMITTED SEPARATELY
--------------------------------------
On its own session, so a rolled-back data transaction cannot erase the evidence
that the attempt happened. The failure log is exactly what you need when the
data did not land.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import (
    AnalyticsRollupRun,
    DailyKpiSnapshot,
    DailyUnmappedSales,
    SkuDailySales,
    SkuDailyStock,
)
from app.services.analytics_rollup import AnalyticsRollupService, is_today
from app.services.timeframe import to_jst_date

log = get_logger(__name__)
SessionFactory = async_sessionmaker[AsyncSession]

#: Arbitrary but fixed. Postgres advisory locks are a single global namespace,
#: so this must not collide with any other lock the app takes.
ROLLUP_LOCK_KEY = 0x524F4C4C  # "ROLL"

#: Days the nightly repair walks back over. Long enough to cover any window the
#: incremental path could plausibly have missed, short enough that the job stays
#: minutes rather than hours as history grows.
DEFAULT_REPAIR_DAYS = 35

#: Ceiling on one run, so a job cannot sit rebuilding a year of history inside
#: an HTTP request. Exceeding it is REPORTED, never silently truncated.
DEFAULT_MAX_DAYS = 120


@dataclass(slots=True)
class RebuildOutcome:
    days_rebuilt: int = 0
    first_date: date | None = None
    last_date: date | None = None
    skipped_locked: bool = False
    remaining_days: int = 0
    pruned_rows: int = 0
    error: str | None = None

    @property
    def status(self) -> str:
        if self.error:
            return "failed"
        if self.skipped_locked:
            return "skipped"
        return "success"


async def _record_run(
    factory: SessionFactory,
    *,
    job_name: str,
    started_at: datetime,
    outcome: RebuildOutcome,
    triggered_by: str | None,
) -> None:
    """Its own session and its own commit — see the module docstring."""
    async with factory() as session, session.begin():
        session.add(
            AnalyticsRollupRun(
                job_name=job_name,
                first_date=outcome.first_date,
                last_date=outcome.last_date,
                days_rebuilt=outcome.days_rebuilt,
                status=outcome.status,
                error=outcome.error[:2000] if outcome.error else None,
                started_at=started_at,
                completed_at=datetime.now(UTC),
                triggered_by=triggered_by,
            )
        )


async def _plan_days(
    factory: SessionFactory,
    *,
    from_date: date | None,
    to_date: date | None,
    repair_days: int | None,
    now: datetime,
) -> list[date]:
    today = to_jst_date(now)
    if from_date:
        last = to_date or today
        if last < from_date:
            return []
        return [from_date + timedelta(days=i) for i in range((last - from_date).days + 1)]

    if repair_days:
        first = today - timedelta(days=repair_days - 1)
        return [first + timedelta(days=i) for i in range(repair_days)]

    async with factory() as session:
        service = AnalyticsRollupService(session)
        watermark = await service.last_success_at()
        return await service.dates_to_rebuild(watermark)


async def _prune(factory: SessionFactory, before: date) -> int:
    """Drop rollup rows older than `before`. Never touches source tables."""
    removed = 0
    async with factory() as session, session.begin():
        for model in (SkuDailyStock, SkuDailySales, DailyUnmappedSales, DailyKpiSnapshot):
            result = await session.execute(delete(model).where(model.stat_date < before))
            removed += int(getattr(result, "rowcount", 0) or 0)
    return removed


async def run(
    *,
    from_date: date | None = None,
    to_date: date | None = None,
    repair_days: int | None = None,
    max_days: int = DEFAULT_MAX_DAYS,
    prune_before: date | None = None,
    dry_run: bool = False,
    job_name: str = "manual",
    triggered_by: str | None = None,
    now: datetime | None = None,
    session_factory: SessionFactory | None = None,
) -> RebuildOutcome:
    factory = session_factory or async_session_factory
    started_at = now or datetime.now(UTC)
    outcome = RebuildOutcome()

    # Held for the whole run by a session of its own; released when it closes.
    async with factory() as lock_session:
        locked = await lock_session.scalar(
            text("SELECT pg_try_advisory_lock(:key)"), {"key": ROLLUP_LOCK_KEY}
        )
        if not locked:
            outcome.skipped_locked = True
            log.info("rollup.skipped_locked", job=job_name)
            await _record_run(
                factory,
                job_name=job_name,
                started_at=started_at,
                outcome=outcome,
                triggered_by=triggered_by,
            )
            return outcome

        try:
            days = await _plan_days(
                factory,
                from_date=from_date,
                to_date=to_date,
                repair_days=repair_days,
                now=started_at,
            )
            capped = days[:max_days] if max_days > 0 else days
            outcome.remaining_days = len(days) - len(capped)
            if outcome.remaining_days:
                # Never a silent truncation: a run that quietly did 120 of 400
                # days reads as "rebuilt" on every dashboard there is.
                log.warning(
                    "rollup.capped",
                    job=job_name,
                    days_total=len(days),
                    days_this_run=len(capped),
                    remaining=outcome.remaining_days,
                )

            if dry_run:
                log.info(
                    "rollup.dry_run",
                    job=job_name,
                    days=len(capped),
                    first=str(capped[0]) if capped else None,
                    last=str(capped[-1]) if capped else None,
                )
                outcome.first_date = capped[0] if capped else None
                outcome.last_date = capped[-1] if capped else None
                return outcome

            for day in capped:
                # One transaction PER DAY: a failure on day 60 leaves days 1-59
                # durable rather than discarding the whole backfill.
                async with factory() as session, session.begin():
                    await AnalyticsRollupService(session).rebuild_day(
                        day, measure_drift=is_today(day, started_at)
                    )
                outcome.days_rebuilt += 1
                outcome.first_date = outcome.first_date or day
                outcome.last_date = day

            if prune_before:
                outcome.pruned_rows = await _prune(factory, prune_before)

        except Exception as exc:  # recorded on the run row; the caller decides
            outcome.error = repr(exc)
            log.exception("rollup.failed", job=job_name, days_done=outcome.days_rebuilt)
        finally:
            await lock_session.execute(
                text("SELECT pg_advisory_unlock(:key)"), {"key": ROLLUP_LOCK_KEY}
            )

    if not dry_run:
        await _record_run(
            factory,
            job_name=job_name,
            started_at=started_at,
            outcome=outcome,
            triggered_by=triggered_by,
        )

    log.info(
        "rollup.done" if not outcome.error else "rollup.error",
        job=job_name,
        status=outcome.status,
        days_rebuilt=outcome.days_rebuilt,
        first=str(outcome.first_date) if outcome.first_date else None,
        last=str(outcome.last_date) if outcome.last_date else None,
        remaining_days=outcome.remaining_days,
        pruned_rows=outcome.pruned_rows,
    )
    return outcome


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=UTC).date()


def main() -> None:
    p = argparse.ArgumentParser(description="Rebuild daily analytics rollups")
    p.add_argument("--from", dest="from_date", type=_parse_date, help="YYYY-MM-DD (JST)")
    p.add_argument("--to", dest="to_date", type=_parse_date, help="YYYY-MM-DD (JST)")
    p.add_argument(
        "--repair-days",
        type=int,
        help=f"Rebuild a trailing window unconditionally (nightly job uses {DEFAULT_REPAIR_DAYS}).",
    )
    p.add_argument("--max-days", type=int, default=DEFAULT_MAX_DAYS, help="0 = unlimited.")
    p.add_argument("--prune-before", type=_parse_date, help="Delete rollup rows before this date.")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    configure_logging("INFO")
    outcome = asyncio.run(
        run(
            from_date=args.from_date,
            to_date=args.to_date,
            repair_days=args.repair_days,
            max_days=args.max_days,
            prune_before=args.prune_before,
            dry_run=args.dry_run,
            job_name="manual",
            triggered_by="cli",
        )
    )
    sys.exit(1 if outcome.error else 0)


if __name__ == "__main__":
    main()
