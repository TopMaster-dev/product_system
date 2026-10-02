"""Read-only: is production in the state the 検収 expects? One command, one verdict.

The post-release checks of docs/32 §5-2 are five separate CLIs, and three of
them exit 0 whatever they find — they were written to be read, not judged. On
the morning of a review that means reading five reports under time pressure,
and `recompute_snapshots` prints its result as one JSON line thousands of SKUs
long. This runs the same checks through the same code and prints one line each:

    [OK] マイグレーション      現在 0014 / head 0014
    [NG] 在庫スナップショット  乖離 3件 ...

Every check runs in its own `READ ONLY` transaction, so nothing here can write
even by mistake, and a check that crashes is reported as NG without aborting
the others. The snapshot check uses `drift_stmt`, not `recompute`: the latter
locks every snapshot row until it finishes, which would hold up order ingestion
on a live system.

Exit 0 only when every check passes.

    powershell -File scripts/run_cli.ps1 -Cli verify_production_health
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.cli.inspect_query_plans import plan_statements
from app.cli.inspect_scheduler_health import (
    DEFAULT_DAYS as SCHEDULER_DAYS,
)
from app.cli.inspect_scheduler_health import (
    findings,
    rollup_days,
    velocity_freshness,
)
from app.cli.inspect_stock_event_damage import (
    find_unfanned_parent_events,
    find_unmanaged_events,
)
from app.cli.recompute_snapshots import drift_stmt
from app.cli.verify_analytics_totals import DEFAULT_DAYS, whole_days_before_today
from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import InventorySnapshot
from app.services.analytics_audit import reconcile_period

log = get_logger(__name__)

REPO_ROOT = Path(__file__).resolve().parents[2]

SessionFactory = async_sessionmaker[AsyncSession]


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    summary: str
    #: The CLI that prints the full report, named on NG.
    detail: str
    #: Context worth reading even when the check passes.
    notes: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Outcome:
    ok: bool
    summary: str
    notes: tuple[str, ...] = ()


def script_head() -> str | None:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    return ScriptDirectory.from_config(cfg).get_current_head()


def _yen(amount: Decimal) -> str:
    return f"{int(amount):,}円"


async def check_migration(session: AsyncSession) -> Outcome:
    current = await session.scalar(text("SELECT version_num FROM alembic_version"))
    head = script_head()
    return Outcome(current == head, f"現在 {current} / head {head}")


def check_totals(days: int) -> Callable[[AsyncSession], Awaitable[Outcome]]:
    async def check(session: AsyncSession) -> Outcome:
        period = whole_days_before_today(days)
        result = await reconcile_period(session, period)
        span = f"{period.first_day}〜{period.last_day}"

        unmapped = result.raw.unmapped_sales_jpy
        total = result.kpis.total_with_unmapped_jpy
        share = f"{unmapped / total:.1%}" if total else "-"
        notes = (f"未マッピング売上 {_yen(unmapped)} / 売上全体の {share} ({span})",)

        if result.agrees and not result.missing_days:
            return Outcome(True, f"{span} / {len(result.measures)}指標すべて一致", notes)
        problems = [f"不一致: {m.label}" for m in result.disagreements]
        if result.missing_days:
            problems.append(f"集計行が無い日 {len(result.missing_days)}日")
        return Outcome(False, f"{span} / {' / '.join(problems)}", notes)

    return check


async def check_scheduler(session: AsyncSession) -> Outcome:
    now = datetime.now(UTC)
    health = await rollup_days(session, days=SCHEDULER_DAYS, now=now)
    latest, _ = await velocity_freshness(session)
    found = findings(health, latest_velocity=latest, now=now)
    if found:
        return Outcome(False, " / ".join(found))
    runs = sum(d.runs for d in health)
    age = (now - latest).total_seconds() / 3600 if latest else 0.0
    return Outcome(
        True, f"直近{len(health)}日 実行{runs}回・失敗0件 / 販売速度 {age:.1f}時間前に更新"
    )


async def check_indexes(session: AsyncSession) -> Outcome:
    checks = await plan_statements(session)
    missing = [c.label for c in checks if c.missing_index]
    if missing:
        return Outcome(False, f"索引が無いクエリ {len(missing)}件: {', '.join(missing)}")
    return Outcome(True, f"{len(checks)}クエリすべて索引あり")


async def check_snapshots(session: AsyncSession) -> Outcome:
    drifted = (await session.execute(drift_stmt())).all()
    skus = await session.scalar(select(func.count()).select_from(InventorySnapshot)) or 0
    if not drifted:
        return Outcome(True, f"{skus:,}SKU / 乖離0件")
    sample = ", ".join(str(row[0]) for row in drifted[:5])
    return Outcome(False, f"乖離 {len(drifted)}件 (master_sku_id: {sample})")


async def check_stock_events(session: AsyncSession) -> Outcome:
    damaged = await find_unmanaged_events(session) + await find_unfanned_parent_events(session)
    outstanding = [d for d in damaged if not d.settled]
    if outstanding:
        delta = sum(d.net_delta for d in outstanding)
        return Outcome(False, f"要対応 {len(outstanding)}SKU / 在庫のずれ {delta:+}")
    settled = len(damaged) - len(outstanding)
    note = f" — 在庫0で決着済みの過去履歴 {settled}SKU は対象外" if settled else ""
    return Outcome(True, f"要対応0件{note}")


async def run_check(
    factory: SessionFactory,
    name: str,
    detail: str,
    check: Callable[[AsyncSession], Awaitable[Outcome]],
) -> Check:
    """One check, one read-only transaction. A crash is a NG result rather
    than an abort: the other checks still say whether production is usable,
    and a review morning is not the time to rerun everything one by one."""
    try:
        async with factory() as session:
            await session.execute(text("SET TRANSACTION READ ONLY"))
            outcome = await check(session)
    except Exception as exc:
        log.exception("production_health.check_crashed", check=name)
        return Check(name, False, f"確認できませんでした: {exc!r}"[:300], detail)
    return Check(name, outcome.ok, outcome.summary, detail, outcome.notes)


def _detail(cli: str, args: str = "") -> str:
    suffix = f' -Args "{args}"' if args else ""
    return f"powershell -File scripts/run_cli.ps1 -Cli {cli}{suffix}"


async def collect(*, days: int, session_factory: SessionFactory | None = None) -> list[Check]:
    factory = session_factory or async_session_factory
    plan = [
        ("マイグレーション", "py -m alembic current", check_migration),
        ("売上数値の突合", _detail("verify_analytics_totals"), check_totals(days)),
        ("定期ジョブ", _detail("inspect_scheduler_health"), check_scheduler),
        ("索引", _detail("inspect_query_plans"), check_indexes),
        (
            "在庫スナップショット",
            _detail("recompute_snapshots", "--all --dry-run"),
            check_snapshots,
        ),
        ("在庫イベント", _detail("inspect_stock_event_damage"), check_stock_events),
    ]
    return [await run_check(factory, name, detail, fn) for name, detail, fn in plan]


def print_report(checks: list[Check], *, now: datetime) -> None:
    jst = now + timedelta(hours=9)
    print("\n  === 本番環境の健全性チェック — 読み取りのみ ===")
    print(f"  {jst:%Y-%m-%d %H:%M} JST\n")
    width = max(len(c.name) for c in checks)
    for c in checks:
        mark = "[OK]" if c.ok else "[NG]"
        print(f"  {mark} {c.name}{'　' * (width - len(c.name))}  {c.summary}")

    notes = [n for c in checks for n in c.notes]
    if notes:
        print()
        for note in notes:
            print(f"  参考: {note}")

    failed = [c for c in checks if not c.ok]
    if not failed:
        print("\n  すべて合格です。")
        return
    print(f"\n  ★ 不合格 {len(failed)}件。詳細は次で確認してください:")
    for c in failed:
        print(f"    {c.name}: {c.detail}")


async def run(*, days: int = DEFAULT_DAYS, session_factory: SessionFactory | None = None) -> int:
    checks = await collect(days=days, session_factory=session_factory)
    print_report(checks, now=datetime.now(UTC))
    log.info(
        "production_health.done",
        ok=all(c.ok for c in checks),
        failed=[c.name for c in checks if not c.ok],
    )
    return 0 if all(c.ok for c in checks) else 1


def main() -> None:
    p = argparse.ArgumentParser(description="Read-only post-release health check")
    p.add_argument("--days", type=int, default=DEFAULT_DAYS, help="whole JST days to reconcile")
    args = p.parse_args()
    configure_logging("INFO")
    sys.exit(asyncio.run(run(days=args.days)))


if __name__ == "__main__":
    main()
