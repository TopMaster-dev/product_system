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
    BundleComponent,
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


def compute_bundle_available(components: list[tuple[int, int]]) -> int:
    """Derived bundle availability from (on_hand, quantity_per) per component:
    max(0, min over components of floor(on_hand / quantity_per)). Empty -> 0.

    This is the pushable quantity for a set/組み合わせ or shared-stock parent —
    clamped at 0 so a component going negative never advertises negative stock.
    """
    if not components:
        return 0
    return max(0, min(on_hand // max(quantity_per, 1) for on_hand, quantity_per in components))


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

    # ---------- bundle / shared-stock ----------

    async def resolve_consumption(self, master_sku_id: int) -> list[tuple[int, int]]:
        """The (component_master_sku_id, quantity_per) list to decrement for ONE
        unit ordered of this master. A bundle/shared-stock parent fans out to its
        components; a normal SKU is itself with quantity_per=1.

        The fan-out reuses one EventSource for every component — master_sku_id is
        part of uq_inventory_event_source, so the component events don't collide.
        """
        result = await self._session.execute(
            select(
                BundleComponent.component_master_sku_id,
                BundleComponent.quantity_per,
            ).where(BundleComponent.bundle_master_sku_id == master_sku_id)
        )
        components = [(cid, qp) for cid, qp in result.all()]
        return components or [(master_sku_id, 1)]

    async def get_bundle_available(self, bundle_master_sku_id: int) -> int:
        """Derived availability for a bundle/shared-stock parent (compute-on-read).
        A master with no components returns its own snapshot (i.e. a normal SKU)."""
        result = await self._session.execute(
            select(
                BundleComponent.component_master_sku_id,
                BundleComponent.quantity_per,
            ).where(BundleComponent.bundle_master_sku_id == bundle_master_sku_id)
        )
        comps = result.all()
        if not comps:
            return await self.get_current_stock(bundle_master_sku_id)
        snap_result = await self._session.execute(
            select(InventorySnapshot.master_sku_id, InventorySnapshot.on_hand_qty).where(
                InventorySnapshot.master_sku_id.in_([cid for cid, _ in comps])
            )
        )
        on_hand: dict[int, int] = {mid: q for mid, q in snap_result.all()}  # noqa: C416
        return compute_bundle_available([(on_hand.get(cid, 0), qp) for cid, qp in comps])

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
