"""BigQuery integration — daily export with pluggable backend.

The `BigQueryClient` Protocol abstracts the destination so tests can use an
in-memory backend, while production uses google-cloud-bigquery. The export
service is unaware of which backend is wired in.
"""

from app.bigquery.client_base import BigQueryClient, BigQueryTable
from app.bigquery.in_memory_client import InMemoryBigQueryClient

__all__ = ["BigQueryClient", "BigQueryTable", "InMemoryBigQueryClient", "get_bigquery_client"]


def get_bigquery_client() -> BigQueryClient:
    """Return the configured BigQuery client.

    Falls back to in-memory when no `BIGQUERY_DATASET` is configured —
    suitable for local dev and tests.
    """
    from app.config import get_settings

    settings = get_settings()
    if not settings.bigquery_dataset or not settings.gcp_project_id:
        return InMemoryBigQueryClient()
    from app.bigquery.google_client import GoogleBigQueryClient

    return GoogleBigQueryClient(
        project_id=settings.gcp_project_id,
        dataset=settings.bigquery_dataset,
    )
