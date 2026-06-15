"""Google Cloud BigQuery client — used in production deployments.

This module imports `google.cloud.bigquery` lazily so the rest of the app
can run locally without the optional `[gcp]` extras installed.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from app.bigquery.client_base import BigQueryTable
from app.logging import get_logger

log = get_logger(__name__)


class GoogleBigQueryClient:
    """Thin wrapper over `google.cloud.bigquery.Client`.

    Phase 1-A keeps the schema management out of band (Terraform). This
    client only writes rows into pre-existing tables.
    """

    def __init__(self, *, project_id: str, dataset: str) -> None:
        try:
            # Different mypy versions emit different codes here
            # (import-untyped on 2.1+, import-not-found on 1.13 to 1.14),
            # so the un-coded ignore suppresses both consistently.
            from google.cloud import bigquery  # type: ignore
        except ImportError as exc:  # pragma: no cover - extras-gated
            raise RuntimeError(
                "google-cloud-bigquery is not installed; install the [gcp] extra"
            ) from exc

        self._bigquery = bigquery
        self._client = bigquery.Client(project=project_id)
        self._project_id = project_id
        self._dataset = dataset

    async def load_rows(
        self,
        table: BigQueryTable,
        rows: Iterable[dict[str, Any]],
        *,
        write_mode: str,
    ) -> int:
        bq = self._bigquery
        disposition = (
            bq.WriteDisposition.WRITE_TRUNCATE
            if write_mode == "truncate"
            else bq.WriteDisposition.WRITE_APPEND
        )
        materialized = list(rows)
        if not materialized:
            log.info("bq.load.empty", table=table.name, mode=write_mode)
            return 0

        table_ref = f"{self._project_id}.{self._dataset}.{table.name}"
        job_config = bq.LoadJobConfig(
            write_disposition=disposition,
            source_format=bq.SourceFormat.NEWLINE_DELIMITED_JSON,
            autodetect=False,
        )
        # Synchronous load_table_from_json; wrap in asyncio.to_thread for asyncio compat.
        import asyncio

        def _run() -> int:
            job = self._client.load_table_from_json(materialized, table_ref, job_config=job_config)
            job.result()
            return job.output_rows or len(materialized)

        count = await asyncio.to_thread(_run)
        log.info("bq.load", table=table.name, mode=write_mode, rows=count)
        return int(count)
