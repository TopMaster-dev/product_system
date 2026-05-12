"""In-memory BigQuery client for local dev and tests."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.bigquery.client_base import BigQueryTable
from app.logging import get_logger

log = get_logger(__name__)


class InMemoryBigQueryClient:
    """Append-only / truncate-able in-memory store keyed by table name.

    Inspect `client.tables[table.name]` to verify what was exported in tests.
    """

    def __init__(self) -> None:
        self.tables: dict[str, list[dict[str, Any]]] = {}

    async def load_rows(
        self,
        table: BigQueryTable,
        rows: Iterable[dict[str, Any]],
        *,
        write_mode: str,
    ) -> int:
        materialized = list(rows)
        if write_mode == "truncate":
            self.tables[table.name] = list(materialized)
        elif write_mode == "append":
            self.tables.setdefault(table.name, []).extend(materialized)
        else:
            raise ValueError(f"unsupported write_mode {write_mode!r}")
        log.info("bq.load", table=table.name, mode=write_mode, rows=len(materialized))
        return len(materialized)
