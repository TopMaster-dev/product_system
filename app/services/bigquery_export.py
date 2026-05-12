"""BigQuery export service — daily incremental + snapshot.

Design:
- Each source table is exported as either INCREMENTAL (watermark on updated_at /
  created_at / occurred_at) or SNAPSHOT (full reload). The mode per table is
  fixed in `TABLE_SPECS`.
- For incremental exports, `bigquery_export_runs` records (since, until)
  watermarks. The next run uses the previous success's `until` as its `since`,
  guaranteeing no gaps and no overlaps.
- The (table_name, until) UNIQUE constraint prevents two parallel runs from
  writing the same window: the second attempt fails fast with IntegrityError.
- Snapshots ignore the watermark (full reload each time) but still record the
  run so we have a history of when full snapshots were taken.
- Failures persist as `status=failed` rows; the next attempt picks a window
  from the LAST SUCCESSFUL run's `until` so a failed run does NOT advance the
  watermark — the data is retried.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.bigquery import BigQueryClient, BigQueryTable
from app.logging import get_logger
from app.models import (
    BigQueryExportRun,
    ChannelSkuMapping,
    InventoryEvent,
    InventorySnapshot,
    MasterSku,
    Order,
    OrderItem,
)

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TableSpec:
    name: str
    mode: str  # "incremental" | "snapshot"
    partition_field: str | None = None


TABLE_SPECS: tuple[TableSpec, ...] = (
    TableSpec("master_skus", "incremental"),
    TableSpec("channel_sku_mappings", "incremental"),
    TableSpec("orders", "incremental", partition_field="ordered_at"),
    TableSpec("order_items", "incremental"),
    TableSpec("inventory_events", "incremental", partition_field="occurred_at"),
    TableSpec("inventory_snapshots", "snapshot"),
)


@dataclass(frozen=True, slots=True)
class ExportResult:
    table_name: str
    mode: str
    rows: int
    since: datetime | None
    until: datetime
    skipped: bool = False
    error: str | None = None


class BigQueryExportService:
    def __init__(self, session: AsyncSession, bq_client: BigQueryClient) -> None:
        self._session = session
        self._bq = bq_client

    async def export_all(self, *, until: datetime | None = None) -> list[ExportResult]:
        """Run the full daily export pipeline. Returns per-table outcomes."""
        until = until or datetime.now(UTC)
        results: list[ExportResult] = []
        for spec in TABLE_SPECS:
            result = await self._export_table_with_dup_guard(spec, until)
            results.append(result)
        return results

    async def _export_table_with_dup_guard(self, spec: TableSpec, until: datetime) -> ExportResult:
        """SAVEPOINT-scoped attempt so a UNIQUE collision on (table, until)
        rolls back only the run-claim insert, not the surrounding transaction.
        """
        try:
            async with self._session.begin_nested():
                return await self._export_table(spec, until)
        except IntegrityError as exc:
            if "uq_bq_export_table_until" in str(exc.orig):
                log.info("bq_export.duplicate_window", table=spec.name, until=str(until))
                return ExportResult(
                    table_name=spec.name,
                    mode=spec.mode,
                    rows=0,
                    since=None,
                    until=until,
                    skipped=True,
                )
            raise

    async def _export_table(self, spec: TableSpec, until: datetime) -> ExportResult:
        since = (
            await self._last_success_watermark(spec.name) if spec.mode == "incremental" else None
        )

        run = BigQueryExportRun(
            table_name=spec.name,
            mode=spec.mode,
            since=since,
            until=until,
            status="running",
            row_count=0,
        )
        self._session.add(run)
        await self._session.flush()  # claim the (table, until) slot

        try:
            rows = await self._fetch_rows(spec, since, until)
            write_mode = "truncate" if spec.mode == "snapshot" else "append"
            count = await self._bq.load_rows(
                BigQueryTable(name=spec.name, partition_field=spec.partition_field),
                rows,
                write_mode=write_mode,
            )
        except Exception as exc:
            run.status = "failed"
            run.error = repr(exc)
            run.completed_at = datetime.now(UTC)
            await self._session.flush()
            log.exception("bq_export.failed", table=spec.name)
            return ExportResult(
                table_name=spec.name,
                mode=spec.mode,
                rows=0,
                since=since,
                until=until,
                error=str(exc),
            )

        run.row_count = count
        run.status = "success"
        run.completed_at = datetime.now(UTC)
        await self._session.flush()
        return ExportResult(
            table_name=spec.name, mode=spec.mode, rows=count, since=since, until=until
        )

    async def _last_success_watermark(self, table_name: str) -> datetime | None:
        result = await self._session.execute(
            select(BigQueryExportRun.until)
            .where(
                BigQueryExportRun.table_name == table_name,
                BigQueryExportRun.status == "success",
            )
            .order_by(BigQueryExportRun.until.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _fetch_rows(
        self,
        spec: TableSpec,
        since: datetime | None,
        until: datetime,
    ) -> list[dict[str, Any]]:
        if spec.name == "master_skus":
            sku_stmt = select(MasterSku)
            if since:
                sku_stmt = sku_stmt.where(MasterSku.updated_at > since)
            sku_stmt = sku_stmt.where(MasterSku.updated_at <= until)
            return [
                _serialize(row) for row in (await self._session.execute(sku_stmt)).scalars().all()
            ]

        if spec.name == "channel_sku_mappings":
            map_stmt = select(ChannelSkuMapping)
            if since:
                map_stmt = map_stmt.where(ChannelSkuMapping.updated_at > since)
            map_stmt = map_stmt.where(ChannelSkuMapping.updated_at <= until)
            return [
                _serialize(row) for row in (await self._session.execute(map_stmt)).scalars().all()
            ]

        if spec.name == "orders":
            ord_stmt = select(Order)
            if since:
                ord_stmt = ord_stmt.where(Order.updated_at > since)
            ord_stmt = ord_stmt.where(Order.updated_at <= until)
            return [
                _serialize(row) for row in (await self._session.execute(ord_stmt)).scalars().all()
            ]

        if spec.name == "order_items":
            # OrderItem has no updated_at; export by parent order's updated_at.
            item_stmt = select(OrderItem).join(Order, Order.id == OrderItem.order_id)
            if since:
                item_stmt = item_stmt.where(Order.updated_at > since)
            item_stmt = item_stmt.where(Order.updated_at <= until)
            return [
                _serialize(row) for row in (await self._session.execute(item_stmt)).scalars().all()
            ]

        if spec.name == "inventory_events":
            ev_stmt = select(InventoryEvent)
            if since:
                ev_stmt = ev_stmt.where(InventoryEvent.created_at > since)
            ev_stmt = ev_stmt.where(InventoryEvent.created_at <= until)
            return [
                _serialize(row) for row in (await self._session.execute(ev_stmt)).scalars().all()
            ]

        if spec.name == "inventory_snapshots":
            snap_stmt = select(InventorySnapshot)
            return [
                _serialize(row) for row in (await self._session.execute(snap_stmt)).scalars().all()
            ]

        raise ValueError(f"unknown table {spec.name}")


def _serialize(row: Any) -> dict[str, Any]:
    """Convert an ORM row to a JSON-serializable dict for BigQuery loads."""
    out: dict[str, Any] = {}
    for col in row.__table__.columns:
        value = getattr(row, col.name)
        if isinstance(value, datetime):
            out[col.name] = value.isoformat()
        elif isinstance(value, Decimal):
            out[col.name] = str(value)
        else:
            out[col.name] = value
    return out


__all__ = ["TABLE_SPECS", "BigQueryExportService", "ExportResult", "TableSpec"]
