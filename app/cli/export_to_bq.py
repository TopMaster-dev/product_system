"""Daily BigQuery export — entrypoint for Cloud Scheduler -> Cloud Run.

    py -m app.cli.export_to_bq                    # normal daily run
    py -m app.cli.export_to_bq --max-windows 0    # unlimited: catch up a backlog

Exits 0 on success, 1 if any per-table export failed.

TRANSACTION SHAPE — this is the point of the module
---------------------------------------------------
Incremental tables are exported in `EXPORT_WINDOW`-sized windows, and **each
window gets its own session and its own commit**. The previous version ran all
six tables inside a single transaction, which meant an OOM kill (SIGKILL, no
exception, no commit) discarded every run record while BigQuery kept the rows it
had already accepted — so the next attempt re-appended them. See
`app.services.bigquery_export` for the full account.

Committing per window makes progress durable: interrupt the process at any point
and every completed window keeps its watermark, so a re-run resumes instead of
restarting. `BigQueryExportService` itself still never commits — the caller owns
transactions, as everywhere else in this codebase.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bigquery import get_bigquery_client
from app.config import get_settings
from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.services import BigQueryExportService
from app.services.bigquery_export import TABLE_SPECS, ExportResult, TableSpec, plan_windows

log = get_logger(__name__)

#: Windows a single scheduled run will attempt per table before stopping and
#: saying so. The daily job needs one; this bounds the damage when the export
#: has been broken for months, so a scheduled run cannot sit in an 82-window
#: catch-up until Cloud Run's request timeout kills it mid-window. A real
#: backlog is caught up deliberately with --max-windows 0.
DEFAULT_MAX_WINDOWS = 7


async def _plan(
    session_factory: async_sessionmaker[Any],
    spec: TableSpec,
    until: datetime,
    bq: Any,
) -> list[datetime]:
    """Window ends for one table, read in a short session of its own so no
    transaction is held open across the BigQuery loads that follow."""
    if spec.mode != "incremental":
        return [until]
    async with session_factory() as session:
        service = BigQueryExportService(session, bq)
        since = await service.last_success_watermark(spec.name)
        if since is None:
            # Never exported: start at the oldest row rather than "everything".
            since = await service.earliest_watermark(spec)
    return plan_windows(since, until)


async def run_export(
    session_factory: async_sessionmaker[Any] = async_session_factory,
    *,
    until: datetime | None = None,
    max_windows: int = DEFAULT_MAX_WINDOWS,
) -> list[ExportResult]:
    """Run the export and return every per-table result (successes and failures).

    `max_windows=0` means unlimited — use it for a deliberate backlog catch-up,
    not for the scheduled run.
    """
    until = until or datetime.now(UTC)
    bq = get_bigquery_client()
    results: list[ExportResult] = []

    for spec in TABLE_SPECS:
        windows = await _plan(session_factory, spec, until, bq)
        capped = windows if max_windows <= 0 else windows[:max_windows]
        if len(capped) < len(windows):
            # Never truncate silently: a run that quietly did 7 of 82 windows
            # reads as "export succeeded" on every dashboard there is.
            log.warning(
                "bq_export.backlog_capped",
                table=spec.name,
                windows_total=len(windows),
                windows_this_run=len(capped),
                remaining=len(windows) - len(capped),
                hint="run `py -m app.cli.export_to_bq --max-windows 0` to catch up",
            )

        for index, window_end in enumerate(capped, start=1):
            # One session, one transaction, one commit per window.
            async with session_factory() as session, session.begin():
                service = BigQueryExportService(session, bq)
                result = await service.export_table(spec, window_end)
            results.append(result)

            if result.error:
                log.error(
                    "bq_export.table_failed",
                    table=spec.name,
                    window=f"{index}/{len(capped)}",
                    until=str(window_end),
                    error=result.error,
                )
                # Stop this table here. Later windows would export data whose
                # predecessor is missing, and the watermark cannot advance past
                # a gap anyway.
                break

            log.info(
                "bq_export.window_done",
                table=spec.name,
                window=f"{index}/{len(capped)}",
                mode=result.mode,
                rows=result.rows,
                until=str(window_end),
                skipped=result.skipped,
            )

        remaining = len(windows) - len(capped)
        if remaining and results and results[-1].table_name == spec.name:
            # Carry the backlog out to the caller. "Nothing failed" and "the
            # export is current" are different claims, and conflating them is
            # what let a broken export look healthy for three months.
            results[-1] = replace(results[-1], remaining_windows=remaining)

    return results


async def run(
    session_factory: async_sessionmaker[Any] = async_session_factory,
    *,
    until: datetime | None = None,
    max_windows: int = DEFAULT_MAX_WINDOWS,
) -> int:
    results = await run_export(session_factory, until=until, max_windows=max_windows)
    return 1 if any(r.error for r in results) else 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Export source tables to BigQuery")
    parser.add_argument(
        "--max-windows",
        type=int,
        default=DEFAULT_MAX_WINDOWS,
        help="Windows per table this run; 0 = unlimited (backlog catch-up).",
    )
    parser.add_argument(
        "--allow-in-memory",
        action="store_true",
        help="Permit running with no BigQuery dataset configured (tests/dev only).",
    )
    args = parser.parse_args()
    settings = get_settings()
    configure_logging(settings.app_log_level)

    # `get_bigquery_client()` returns an in-memory stub when the dataset or
    # project is unset, which is right for tests and catastrophic for a manual
    # backfill: the run would report success, write nothing, and leave someone
    # confident the backlog was cleared. Refuse instead of pretending.
    if not args.allow_in_memory and not (settings.bigquery_dataset and settings.gcp_project_id):
        sys.exit(
            "BIGQUERY_DATASET / GCP_PROJECT_ID が未設定です。"
            " このまま実行すると BigQuery には何も書き込まれません"
            " (in-memory クライアントにフォールバックします)。"
            " scripts/run_cli.ps1 経由で実行するか、環境変数を設定してください。"
        )

    sys.exit(asyncio.run(run(max_windows=args.max_windows)))


if __name__ == "__main__":
    main()
