"""InventoryService — event-sourced inventory mutations.

Design invariants:
- Every change appends an immutable row to `inventory_events`.
- `inventory_snapshots` is a materialized projection of those events; the events
  are the source of truth.
- All snapshot writes go through `SELECT FOR UPDATE` to serialize concurrent
  decrements against the same SKU.
- Order-driven events (order_consumed / cancellation_returned) carry the
  source identifiers, so the UNIQUE constraint
  (event_type, source_channel, source_order_id, source_line_id)
  prevents duplicate application from at-least-once delivery.
- The append and the snapshot update happen in the same transaction managed
  by the caller; the service never commits implicitly.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    InventoryEvent,
    InventoryEventTypeEnum,
    InventorySnapshot,
    MasterSku,
)
from app.services.exceptions import (
    InventoryInsufficientError,
    MasterSkuNotFoundError,
)


@dataclass(frozen=True, slots=True)
class EventSource:
    """Identifies the originating order line for idempotency."""

    channel: str
    order_id: str
    line_id: str


class InventoryService:
    """Inventory mutations against the event log + snapshot."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---------- public API ----------

    async def consume_for_order_line(
        self,
        *,
        master_sku_id: int,
        quantity: int,
        source: EventSource,
        occurred_at: datetime | None = None,
    ) -> InventoryEvent | None:
        """Decrement stock for a single order line.

        Returns the new event, or None if the same source was already applied
        (idempotent no-op).
        """
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        return await self._append_sourced_event(
            event_type=InventoryEventTypeEnum.ORDER_CONSUMED,
            master_sku_id=master_sku_id,
            quantity_delta=-quantity,
            source=source,
            occurred_at=occurred_at,
        )

    async def cancel_order_line(
        self,
        *,
        master_sku_id: int,
        quantity: int,
        source: EventSource,
        occurred_at: datetime | None = None,
    ) -> InventoryEvent | None:
        """Compensate a previously-consumed order line by adding stock back."""
        if quantity <= 0:
            raise ValueError("quantity must be positive")
        return await self._append_sourced_event(
            event_type=InventoryEventTypeEnum.CANCELLATION_RETURNED,
            master_sku_id=master_sku_id,
            quantity_delta=quantity,
            source=source,
            occurred_at=occurred_at,
        )

    async def manual_adjust(
        self,
        *,
        master_sku_id: int,
        quantity_delta: int,
        reason: str,
        operator: str,
        occurred_at: datetime | None = None,
    ) -> InventoryEvent:
        """Apply a manual adjustment with a recorded reason and operator."""
        if quantity_delta == 0:
            raise ValueError("quantity_delta must be non-zero")

        snapshot = await self._lock_or_create_snapshot(master_sku_id)
        projected = snapshot.on_hand_qty + quantity_delta
        if projected < 0:
            raise InventoryInsufficientError(
                f"manual_adjust would leave master_sku_id={master_sku_id} at {projected}",
            )

        event = InventoryEvent(
            master_sku_id=master_sku_id,
            event_type=InventoryEventTypeEnum.MANUAL_ADJUST,
            quantity_delta=quantity_delta,
            reason=reason,
            operator=operator,
            occurred_at=occurred_at or datetime.now(UTC),
        )
        self._session.add(event)
        await self._session.flush()
        snapshot.on_hand_qty = projected
        snapshot.last_event_id = event.id
        await self._session.flush()
        return event

    async def get_current_stock(self, master_sku_id: int) -> int:
        result = await self._session.execute(
            select(InventorySnapshot.on_hand_qty).where(
                InventorySnapshot.master_sku_id == master_sku_id,
            ),
        )
        qty = result.scalar_one_or_none()
        return qty or 0

    # ---------- internal helpers ----------

    async def _append_sourced_event(
        self,
        *,
        event_type: InventoryEventTypeEnum,
        master_sku_id: int,
        quantity_delta: int,
        source: EventSource,
        occurred_at: datetime | None,
    ) -> InventoryEvent | None:
        # Lock the snapshot row first; this also serializes concurrent
        # decrements for the same master_sku.
        snapshot = await self._lock_or_create_snapshot(master_sku_id)

        event = InventoryEvent(
            master_sku_id=master_sku_id,
            event_type=event_type,
            quantity_delta=quantity_delta,
            source_channel=source.channel,
            source_order_id=source.order_id,
            source_line_id=source.line_id,
            occurred_at=occurred_at or datetime.now(UTC),
        )

        # SAVEPOINT around the insert so a UNIQUE violation rolls back only
        # the duplicate-event attempt, leaving the outer transaction intact.
        try:
            async with self._session.begin_nested():
                self._session.add(event)
                await self._session.flush()
        except IntegrityError as exc:
            if "uq_inventory_event_source" in str(exc.orig):
                return None
            raise

        snapshot.on_hand_qty += quantity_delta
        snapshot.last_event_id = event.id
        await self._session.flush()
        return event

    async def _lock_or_create_snapshot(self, master_sku_id: int) -> InventorySnapshot:
        """Return the snapshot row locked FOR UPDATE, creating it if missing."""
        result = await self._session.execute(
            select(InventorySnapshot)
            .where(InventorySnapshot.master_sku_id == master_sku_id)
            .with_for_update(),
        )
        snapshot = result.scalar_one_or_none()
        if snapshot is not None:
            return snapshot

        # Verify the master_sku exists before creating a snapshot for it.
        sku_exists = await self._session.execute(
            select(MasterSku.id).where(MasterSku.id == master_sku_id),
        )
        if sku_exists.scalar_one_or_none() is None:
            raise MasterSkuNotFoundError(f"master_sku_id={master_sku_id}")

        snapshot = InventorySnapshot(master_sku_id=master_sku_id, on_hand_qty=0)
        self._session.add(snapshot)
        await self._session.flush()
        return snapshot
