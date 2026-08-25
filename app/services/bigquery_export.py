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

WHY INCREMENTAL EXPORTS ARE WINDOWED
------------------------------------
An incremental export covers (last success, now]. When the export itself is
broken, that interval grows without bound — and in production it reached **82
days**, because a missing IAM role stopped every run between 2026-05-31 and
2026-08-21. The first run to get past the permission error then tried to
materialise three months of `inventory_events` at once and was OOM-killed on a
512 MiB container.

The kill is the dangerous part, not the memory. A SIGKILL raises no Python
exception, so nothing marks the run failed and nothing commits — while the rows
BigQuery already accepted stay there, because a BigQuery load job is not
transactional with Postgres. The next attempt reads the same unmoved watermark
and appends the same rows again. With Cloud Scheduler retrying, one bad night
could append several copies.

So the caller (`app.cli.export_to_bq`) splits an incremental export into
`EXPORT_WINDOW`-sized windows and commits each one, via `export_table()` per
window. That gives three properties the single-shot version could not:

* memory is bounded by ONE window regardless of how far behind the export is;
* a kill loses at most one window — every earlier window is already committed,
  and its watermark stands;
* re-running is cheap and safe for completed windows: their (table, until) rows
  are committed, so the UNIQUE constraint skips them.

The residual gap is narrow but real: a process killed after BigQuery accepts a
window but before Postgres commits it will re-append that ONE window on retry.
Closing that completely needs a staging table plus MERGE, which is a larger
change; the exposure here is a single window rather than an entire backlog.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, func, select
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
    ProductCategory,
)

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class TableSpec:
    name: str
    mode: str  # "incremental" | "snapshot"
    partition_field: str | None = None


TABLE_SPECS: tuple[TableSpec, ...] = (
    # Dimension tables are exported as SNAPSHOTs, not incrementally. An
    # incremental export watermarks on updated_at, and ADD COLUMN does not touch
    # updated_at — so a schema change silently leaves the new column NULL in
    # BigQuery forever for every row that is not subsequently edited. Both tables
    # are small (~1k and ~2k rows), so a full reload is cheap and self-healing.
    TableSpec("master_skus", "snapshot"),
    TableSpec("channel_sku_mappings", "snapshot"),
    TableSpec("orders", "incremental", partition_field="ordered_at"),
    TableSpec("order_items", "incremental"),
    TableSpec("inventory_events", "incremental", partition_field="occurred_at"),
    TableSpec("inventory_snapshots", "snapshot"),
    # Tiny (tens of rows) and edited by hand in the admin UI, so a full reload
    # every run is both cheap and self-healing — same reasoning as the two
    # dimension tables above.
    TableSpec("product_categories", "snapshot"),
)

#: How much time one incremental window covers. A day of this shop's activity is
#: a few thousand rows — small enough that peak memory is dominated by the
#: container's baseline rather than the payload, at any backlog depth.
EXPORT_WINDOW = timedelta(days=1)

#: The column each incremental table is watermarked on, used to find where an
#: export with no successful run yet should START. Without a lower bound the
#: first window would be "everything ever", which is the unbounded case the
#: windowing exists to prevent.
_EARLIEST_WATERMARK: dict[str, Any] = {
    "orders": Order.updated_at,
    # OrderItem has no updated_at of its own; it rides its parent order's.
    "order_items": Order.updated_at,
    "inventory_events": InventoryEvent.created_at,
}


def plan_windows(
    since: datetime | None,
    until: datetime,
    *,
    window: timedelta = EXPORT_WINDOW,
) -> list[datetime]:
    """The window END timestamps covering (since, until].

    Each returned value is one `export_table()` call; the service derives that
    window's `since` from the previous window's committed watermark, so the
    windows chain without gaps or overlaps.

    `since is None` means there is no lower bound to divide (an empty source
    table, or a mode that ignores watermarks), so the whole range is one window.
    """
    if since is None or since >= until:
        return [until]
    ends: list[datetime] = []
    cursor = since
    while cursor < until:
        cursor = min(cursor + window, until)
        ends.append(cursor)
    return ends


@dataclass(frozen=True, slots=True)
class ExportResult:
    table_name: str
    mode: str
    rows: int
    since: datetime | None
    until: datetime
    skipped: bool = False
    error: str | None = None
    #: Windows this table still owes after a capped run. Non-zero means the
    #: export is BEHIND even though nothing failed — the state that hid a broken
    #: export for three months, so it has to reach the caller, not just the log.
    remaining_windows: int = 0


class BigQueryExportService:
    def __init__(self, session: AsyncSession, bq_client: BigQueryClient) -> None:
        self._session = session
        self._bq = bq_client

    async def export_all(self, *, until: datetime | None = None) -> list[ExportResult]:
        """Every table in ONE window and ONE transaction.

        Kept for tests and ad-hoc use. Production goes through
        `app.cli.export_to_bq.run_export`, which windows incremental tables and
        commits each window — see this module's docstring for why that matters.
        """
        until = until or datetime.now(UTC)
        results: list[ExportResult] = []
        for spec in TABLE_SPECS:
            result = await self.export_table(spec, until)
            results.append(result)
        return results

    async def export_table(self, spec: TableSpec, until: datetime) -> ExportResult:
        """Export ONE table up to `until`. The caller owns the transaction, so
        committing after this returns is what makes the window durable."""
        return await self._export_table_with_dup_guard(spec, until)

    async def last_success_watermark(self, table_name: str) -> datetime | None:
        """Public: the caller needs it to plan windows before opening the
        per-window transactions."""
        return await self._last_success_watermark(table_name)

    async def earliest_watermark(self, spec: TableSpec) -> datetime | None:
        """The oldest source timestamp, i.e. where a never-yet-exported table
        should begin. Returns None when the table is empty or not incremental —
        `plan_windows` then treats the range as a single window, which is
        correct because there is nothing to divide."""
        column = _EARLIEST_WATERMARK.get(spec.name)
        if column is None:
            return None
        result = await self._session.scalar(select(func.min(column)))
        return result

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

    def _source_query(
        self,
        spec: TableSpec,
        since: datetime | None,
        until: datetime,
    ) -> Select[Any]:
        """The rows this table owes for the window (since, until].

        Counting and fetching MUST come from one definition. When they were
        written separately, "how far behind are we?" and "what will we send?"
        could quietly disagree — the same class of bug as the inventory badges
        that did not match their own list.
        """
        if spec.name == "master_skus":
            sku_stmt = select(MasterSku)
            if since:
                sku_stmt = sku_stmt.where(MasterSku.updated_at > since)
            return sku_stmt.where(MasterSku.updated_at <= until)

        if spec.name == "channel_sku_mappings":
            map_stmt = select(ChannelSkuMapping)
            if since:
                map_stmt = map_stmt.where(ChannelSkuMapping.updated_at > since)
            return map_stmt.where(ChannelSkuMapping.updated_at <= until)

        if spec.name == "orders":
            ord_stmt = select(Order)
            if since:
                ord_stmt = ord_stmt.where(Order.updated_at > since)
            return ord_stmt.where(Order.updated_at <= until)

        if spec.name == "order_items":
            # OrderItem has no updated_at; it rides its parent order's.
            item_stmt = select(OrderItem).join(Order, Order.id == OrderItem.order_id)
            if since:
                item_stmt = item_stmt.where(Order.updated_at > since)
            return item_stmt.where(Order.updated_at <= until)

        if spec.name == "inventory_events":
            # created_at, NOT occurred_at: a backdated cancellation carries the
            # original order's occurred_at and would fall outside every future
            # window, so it would never be exported at all.
            ev_stmt = select(InventoryEvent)
            if since:
                ev_stmt = ev_stmt.where(InventoryEvent.created_at > since)
            return ev_stmt.where(InventoryEvent.created_at <= until)

        if spec.name == "inventory_snapshots":
            return select(InventorySnapshot)

        if spec.name == "product_categories":
            return select(ProductCategory)

        raise ValueError(f"unknown table {spec.name}")

    async def count_source_rows(
        self,
        spec: TableSpec,
        since: datetime | None,
        until: datetime,
    ) -> int:
        """How many rows the window covers, without materialising any of them."""
        stmt = select(func.count()).select_from(self._source_query(spec, since, until).subquery())
        return await self._session.scalar(stmt) or 0

    async def _fetch_rows(
        self,
        spec: TableSpec,
        since: datetime | None,
        until: datetime,
    ) -> list[dict[str, Any]]:
        result = await self._session.execute(self._source_query(spec, since, until))
        return [_serialize(row) for row in result.scalars().all()]


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
