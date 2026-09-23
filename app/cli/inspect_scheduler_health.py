"""Read-only: did the scheduled jobs actually run, two days running? (P2-045)

Cloud Scheduler judges a run by its HTTP status. A job that returns 200 having
done nothing is recorded as a success, and this service has had three outages
that looked exactly like that from the console. So the console is not evidence.

What is evidence is the work leaving a trace in the database:

* `analytics_rollup_runs` — one row per attempt, written in its own session and
  committed independently, so a failed run still records that it happened.
* `sku_velocity.computed_at` — rewritten by every rollup pass that did work.

THE CHECK THAT MATTERS IS COVERAGE, NOT FAILURES

"No failure rows" is the reading that hides an outage: a job that never fired
produces no failures either. The hourly rollup should leave roughly 24 rows a
day, so this counts rows PER JST DAY and names the days that fall short —
absence is the finding, not an empty list.

Cloud Logging is the third source and is not read here; it needs `gcloud`.
docs/32 §4 pairs this output with it.

    powershell -File scripts/run_cli.ps1 -Cli inspect_scheduler_health
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import AnalyticsRollupRun, SkuVelocity
from app.services.timeframe import to_jst_date

log = get_logger(__name__)

DEFAULT_DAYS = 3

#: The hourly job fires 24 times a day. Below this, something skipped — the
#: run itself, or the Scheduler. Not 24, because a deploy or a held advisory
#: lock legitimately costs an hour.
MIN_HOURLY_RUNS = 20

#: The velocity table is rewritten by the rollup, so a stale timestamp means
#: no pass did any work. Two missed hours is past coincidence.
VELOCITY_STALE_HOURS = 3.0


@dataclass
class DayHealth:
    day: date
    runs: int = 0
    successes: int = 0
    failures: int = 0
    #: JST hours elapsed on this day. 24 for a finished day, fewer for today.
    #: Judging a day that is four hours old against a full day's expectation
    #: reports every morning run as a shortfall, and a check that cries wolf
    #: every morning is a check nobody reads.
    hours_elapsed: int = 24
    jobs: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    @property
    def partial(self) -> bool:
        return self.hours_elapsed < 24

    @property
    def expected_runs(self) -> int:
        return round(MIN_HOURLY_RUNS * self.hours_elapsed / 24)

    @property
    def healthy(self) -> bool:
        return self.successes > 0 and self.failures == 0

    @property
    def verdict(self) -> str:
        if self.successes == 0:
            return "★ 成功した実行がありません"
        if self.failures:
            return f"★ 失敗 {self.failures}件"
        if self.runs < self.expected_runs:
            return f"△ 実行回数が少なめ {self.runs}回 / 目安 {self.expected_runs}回"
        return "進行中" if self.partial else "正常"


async def rollup_days(session: AsyncSession, *, days: int, now: datetime) -> list[DayHealth]:
    """Runs per JST day. Bucketed in Python because `started_at` is UTC and the
    business day is JST — a naive ::date would misfile every run before 09:00."""
    since = now - timedelta(days=days)
    rows = await session.execute(
        select(
            AnalyticsRollupRun.started_at,
            AnalyticsRollupRun.status,
            AnalyticsRollupRun.job_name,
        ).where(AnalyticsRollupRun.started_at >= since)
    )

    today = to_jst_date(now)
    # JST is UTC+9; the hour of the JST day that `now` falls in.
    elapsed_today = ((now.hour + 9) % 24) + 1

    buckets: dict[date, DayHealth] = {}
    for offset in range(days):
        day = today - timedelta(days=offset)
        buckets[day] = DayHealth(day=day, hours_elapsed=elapsed_today if day == today else 24)

    for started_at, status, job_name in rows.all():
        day = to_jst_date(started_at)
        health = buckets.get(day)
        if health is None:
            continue
        health.runs += 1
        health.jobs[job_name] += 1
        if status == "success":
            health.successes += 1
        else:
            health.failures += 1

    return [buckets[d] for d in sorted(buckets, reverse=True)]


async def velocity_freshness(session: AsyncSession) -> tuple[datetime | None, int]:
    """(most recent computed_at, rows)."""
    row = (
        await session.execute(
            select(func.max(SkuVelocity.computed_at), func.count()).select_from(SkuVelocity)
        )
    ).one()
    return row[0], int(row[1] or 0)


def report(
    days: list[DayHealth],
    *,
    latest_velocity: datetime | None,
    velocity_rows: int,
    now: datetime,
) -> int:
    print("\n  === 定期ジョブの稼働状況 — 読み取りのみ ===")
    print("  Cloud Scheduler の成功表示ではなく、実際に残った記録で判定しています。\n")

    print(f"    {'日付(JST)':<14}{'実行':>6}{'成功':>6}{'失敗':>6}  判定")
    problems = 0
    for d in days:
        if not d.healthy:
            problems += 1
        print(f"    {d.day.isoformat():<14}{d.runs:>6}{d.successes:>6}{d.failures:>6}  {d.verdict}")

    jobs: dict[str, int] = defaultdict(int)
    for d in days:
        for name, n in d.jobs.items():
            jobs[name] += n
    if jobs:
        print("\n  ジョブ別の実行回数")
        for name, n in sorted(jobs.items(), key=lambda kv: kv[1], reverse=True):
            print(f"    {name:<34}{n:>5}回")

    print("\n  --- 販売速度テーブルの鮮度 ---")
    if latest_velocity is None:
        print("    ★ 一度も計算されていません")
        problems += 1
    else:
        age = (now - latest_velocity).total_seconds() / 3600
        stale = age > VELOCITY_STALE_HOURS
        mark = "★ " if stale else ""
        when = f"{latest_velocity:%Y-%m-%d %H:%M} UTC"
        print(f"    {mark}最終計算 {when} / {age:.1f}時間前 / {velocity_rows}行")
        if stale:
            print("    ロールアップが実質的な処理をしていません。")
            problems += 1

    # Two consecutive days is the P2-045 requirement; state the verdict rather
    # than leaving it to be counted off the table.
    recent = days[:2]
    consecutive = len(recent) == 2 and all(d.healthy for d in recent)
    print("\n  --- 2日連続の稼働  P2-045 ---")
    if consecutive:
        print(f"    充足: {recent[1].day} と {recent[0].day} の両方で成功しています")
    else:
        print("    ★ 未充足。上の表で失敗または未実行の日を確認してください")
        problems += 1

    if problems:
        print(f"\n  ★ 確認が必要な項目 {problems}件")
        print("    Cloud Logging と突き合わせてください — docs/32 §4")
    else:
        print("\n  問題は見つかりませんでした")
    return problems


async def run(*, days: int = DEFAULT_DAYS) -> int:
    now = datetime.now(UTC)
    async with async_session_factory() as session:
        health = await rollup_days(session, days=days, now=now)
        latest, rows = await velocity_freshness(session)

    problems = report(health, latest_velocity=latest, velocity_rows=rows, now=now)
    log.info(
        "scheduler_health.done",
        days=days,
        problems=problems,
        runs=sum(d.runs for d in health),
        failures=sum(d.failures for d in health),
    )
    return 0 if problems == 0 else 1


def main() -> None:
    p = argparse.ArgumentParser(description="Check the scheduled jobs actually ran")
    p.add_argument("--days", type=int, default=DEFAULT_DAYS, help="JST days back to inspect")
    args = p.parse_args()
    configure_logging("INFO")
    sys.exit(asyncio.run(run(days=args.days)))


if __name__ == "__main__":
    main()
