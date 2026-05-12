"""Daily BigQuery export — entrypoint for Cloud Scheduler -> Cloud Run job.

    py -m app.cli.export_to_bq

Exits 0 on success, 1 if any per-table export failed. Cloud Scheduler can
retry the entire job on non-zero exit.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.bigquery import get_bigquery_client
from app.config import get_settings
from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.services import BigQueryExportService


async def run(
    session_factory: async_sessionmaker[Any] = async_session_factory,
    *,
    until: datetime | None = None,
) -> int:
    until = until or datetime.now(UTC)
    bq = get_bigquery_client()
    log = get_logger(__name__)

    async with session_factory() as session, session.begin():
        service = BigQueryExportService(session, bq)
        results = await service.export_all(until=until)

    exit_code = 0
    for r in results:
        if r.error:
            exit_code = 1
            log.error(
                "bq_export.table_failed",
                table=r.table_name,
                error=r.error,
            )
        else:
            log.info(
                "bq_export.table_done",
                table=r.table_name,
                mode=r.mode,
                rows=r.rows,
                skipped=r.skipped,
            )
    return exit_code


def main() -> None:
    configure_logging(get_settings().app_log_level)
    code = asyncio.run(run())
    sys.exit(code)


if __name__ == "__main__":
    main()
