"""Read-only: how does PostgreSQL actually plan the hourly rollup? (P2-043)

`EXPLAIN` without `ANALYZE` — the query is planned, never run.

Two plans per statement, because they answer different questions:

**現在の計画** is what production does today, against today's statistics. It is
the P2-043 evidence: a sequential scan here is a real cost being paid every
hour.

**索引の有無** re-plans with `enable_seqscan = off`. That penalises sequential
scans heavily but cannot remove them — with no usable index the planner still
returns one. So a sequential scan in THIS plan means something stronger: no
index can serve the predicate at all, and the cost only grows with the table.

A query can legitimately be sequential today and indexed-capable — a small
table is genuinely cheaper to scan. The pair is what tells them apart.

    powershell -File scripts/run_cli.ps1 -Cli inspect_query_plans
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import Select, func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import InventoryEvent, Order
from app.services.analytics_rollup import (
    changed_event_days_stmt,
    changed_order_days_stmt,
    day_movement_stmt,
    opening_balance_stmt,
)

log = get_logger(__name__)


def _statements() -> list[tuple[str, str, Select[Any]]]:
    """(label, table under scrutiny, statement). The same builders the rollup
    runs — a copy here would report on the copy."""
    now = datetime.now(UTC)
    since = now - timedelta(hours=1)
    start = now - timedelta(days=1)
    population = list(range(1, 51))
    return [
        ("再構築対象日: 在庫イベント", "inventory_events", changed_event_days_stmt(since)),
        ("再構築対象日: 受注", "orders", changed_order_days_stmt(since)),
        ("日次在庫: 前日までの残高", "inventory_events", opening_balance_stmt(start, population)),
        ("日次在庫: 当日の増減", "inventory_events", day_movement_stmt(start, now, population)),
    ]


async def _explain(session: AsyncSession, stmt: Select[Any], *, no_seqscan: bool) -> str:
    compiled = stmt.compile(dialect=session.bind.dialect, compile_kwargs={"literal_binds": True})
    if no_seqscan:
        await session.execute(text("SET LOCAL enable_seqscan = off"))
    rows = await session.execute(text(f"EXPLAIN {compiled}"))
    plan = "\n".join(str(line[0]) for line in rows.all())
    if no_seqscan:
        await session.execute(text("SET LOCAL enable_seqscan = on"))
    return plan


def _first_line(plan: str) -> str:
    return plan.splitlines()[0].strip() if plan else ""


async def run() -> int:
    async with async_session_factory() as session:
        events = await session.scalar(select(func.count()).select_from(InventoryEvent)) or 0
        orders = await session.scalar(select(func.count()).select_from(Order)) or 0

        print("\n  === ロールアップの実行計画 — EXPLAIN のみ、クエリは実行しません ===")
        print(f"  inventory_events {events:,}行 / orders {orders:,}行")

        findings: list[str] = []
        for label, table, stmt in _statements():
            current = await _explain(session, stmt, no_seqscan=False)
            forced = await _explain(session, stmt, no_seqscan=True)

            seq_now = f"Seq Scan on {table}" in current
            seq_forced = f"Seq Scan on {table}" in forced

            if seq_forced:
                verdict = "★ 索引なし — この述語を処理できる索引が存在しません"
                findings.append(f"{label} ({table})")
            elif seq_now:
                verdict = "現在は全走査 — 索引はあるが、今の行数では全走査の方が安いと判断"
            else:
                verdict = "索引を使用"

            print(f"\n  --- {label} ---")
            print(f"  {verdict}")
            print(f"    現在の計画 : {_first_line(current)}")
            if seq_now != seq_forced:
                print(f"    索引使用時 : {_first_line(forced)}")

    if findings:
        print(f"\n  ★ 索引が無いクエリ {len(findings)}件: {', '.join(findings)}")
        print("    行数が増えるほど毎時の処理時間が線形に伸びます。")
    else:
        print("\n  すべてのクエリに、使用可能な索引があります")

    log.info("query_plans.done", events=events, orders=orders, missing_index=len(findings))
    return 0


def main() -> None:
    argparse.ArgumentParser(description="EXPLAIN the rollup's time-range scans").parse_args()
    configure_logging("INFO")
    sys.exit(asyncio.run(run()))


if __name__ == "__main__":
    main()
