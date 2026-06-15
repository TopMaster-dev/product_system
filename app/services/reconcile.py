"""ReconcileService — CROSS MALL based daily reconciliation orchestrator.

Phase 1-B F1.7.

A ReconcileRun is initialized with a list of `(master_sku_id, current_qty,
target_qty)` tuples (typically produced by F1.8 reconcile_inventory CLI
from CROSS MALL's inventory CSV). For each entry where current != target
we create a ReconcileDiff row; the operator then reviews them in the
admin UI (F3.2) and approves or skips each.

Approving a diff:
- creates a `stocktake` InventoryEvent with quantity_delta = target - current
- updates the InventorySnapshot to the target value
- links applied_event_id to the new event so the UI can show "applied as X"

Skipping a diff just marks the decision as `skipped` (no event, no snapshot
change).

Per client decision D-6, post-approval pushes to Rakuten/Shopify are NOT
fired inside this service — they are batched at run finalization time so
rate limits / retries can be coordinated across SKUs. F1.8 / the admin
finalize endpoint will iterate the approved diffs and call
`InventoryPushService.push_single` for each.

The service does not commit; the caller owns transaction boundaries.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.logging import get_logger
from app.models import (
    InventoryEvent,
    InventoryEventTypeEnum,
    InventorySnapshot,
    ReconcileDiff,
    ReconcileDiffDecisionEnum,
    ReconcileRun,
    ReconcileRunStatusEnum,
)

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class DiffInput:
    """One row's worth of CROSS MALL vs central-DB comparison."""

    master_sku_id: int
    current_qty: int
    target_qty: int


@dataclass(frozen=True, slots=True)
class DiffApplyResult:
    diff: ReconcileDiff
    event: InventoryEvent | None  # None when delta was 0 (no-op)


_RECONCILE_SOURCE_CHANNEL = "reconcile"


class ReconcileService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # ---------- run lifecycle ----------

    async def start_run(
        self,
        *,
        source: str,
        triggered_by: str,
        diffs: Iterable[DiffInput],
        csv_filename: str | None = None,
    ) -> ReconcileRun:
        """Create a new ReconcileRun and its diff rows.

        Only rows where `target_qty != current_qty` are persisted; matching
        rows are silently skipped so the operator's approval queue stays
        focused on actual deltas.

        Status flows: running -> pending_approval immediately after the
        diff rows are flushed, since at that point human action is needed.
        """
        run = ReconcileRun(
            source=source,
            csv_filename=csv_filename,
            status=ReconcileRunStatusEnum.RUNNING.value,
            triggered_by=triggered_by,
        )
        self._session.add(run)
        await self._session.flush()

        diff_count = 0
        for d in diffs:
            delta = d.target_qty - d.current_qty
            if delta == 0:
                continue
            diff = ReconcileDiff(
                reconcile_run_id=run.id,
                master_sku_id=d.master_sku_id,
                current_qty=d.current_qty,
                target_qty=d.target_qty,
                delta=delta,
                decision=ReconcileDiffDecisionEnum.PENDING.value,
            )
            self._session.add(diff)
            diff_count += 1
        await self._session.flush()

        run.diff_count = diff_count
        run.status = (
            ReconcileRunStatusEnum.PENDING_APPROVAL.value
            if diff_count > 0
            else ReconcileRunStatusEnum.APPLIED.value
        )
        if diff_count == 0:
            # No diffs to apply — finalize immediately.
            run.finished_at = datetime.now(UTC)
        await self._session.flush()

        log.info(
            "reconcile.run_started",
            run_id=run.id,
            source=source,
            triggered_by=triggered_by,
            diff_count=diff_count,
        )
        return run

    async def approve_diff(
        self,
        *,
        diff_id: int,
        approved_by: str,
    ) -> DiffApplyResult:
        """Approve a single diff, generating a stocktake event and updating
        the snapshot. Idempotent: re-approving an already-approved diff is a
        no-op that returns the existing applied event."""
        diff = await self._get_diff(diff_id)
        if diff.decision == ReconcileDiffDecisionEnum.APPROVED.value:
            existing_event = (
                (
                    await self._session.execute(
                        select(InventoryEvent).where(InventoryEvent.id == diff.applied_event_id)
                    )
                ).scalar_one_or_none()
                if diff.applied_event_id
                else None
            )
            return DiffApplyResult(diff=diff, event=existing_event)
        if diff.decision == ReconcileDiffDecisionEnum.SKIPPED.value:
            raise RuntimeError(
                f"reconcile diff id={diff_id} was skipped; cannot approve. "
                f"Reopen by resetting decision to pending."
            )

        # Make the stocktake event with source_* fields so the
        # inventory_events UNIQUE prevents double-application across
        # accidental re-approvals.
        snapshot = await self._lock_or_create_snapshot(diff.master_sku_id)
        event = InventoryEvent(
            master_sku_id=diff.master_sku_id,
            event_type=InventoryEventTypeEnum.STOCKTAKE,
            quantity_delta=diff.delta,
            source_channel=_RECONCILE_SOURCE_CHANNEL,
            source_order_id=f"run-{diff.reconcile_run_id}",
            source_line_id=f"diff-{diff.id}",
            reason="CROSS MALL reconciliation approved",
            operator=approved_by,
            occurred_at=datetime.now(UTC),
        )
        self._session.add(event)
        await self._session.flush()

        snapshot.on_hand_qty = diff.target_qty
        snapshot.last_event_id = event.id

        diff.decision = ReconcileDiffDecisionEnum.APPROVED.value
        diff.applied_event_id = event.id
        diff.decided_by = approved_by
        diff.decided_at = datetime.now(UTC)
        await self._session.flush()

        # Tally on the run for the admin UI counter.
        run = await self._get_run(diff.reconcile_run_id)
        run.applied_count = (run.applied_count or 0) + 1
        await self._session.flush()

        log.info(
            "reconcile.diff_approved",
            diff_id=diff.id,
            run_id=diff.reconcile_run_id,
            master_sku_id=diff.master_sku_id,
            delta=diff.delta,
            event_id=event.id,
            approved_by=approved_by,
        )
        return DiffApplyResult(diff=diff, event=event)

    async def skip_diff(
        self,
        *,
        diff_id: int,
        approved_by: str,
    ) -> ReconcileDiff:
        """Mark a diff as skipped — no event, no snapshot change. Reversible
        by resetting decision to pending."""
        diff = await self._get_diff(diff_id)
        if diff.decision == ReconcileDiffDecisionEnum.APPROVED.value:
            raise RuntimeError(
                f"reconcile diff id={diff_id} is already approved; cannot "
                f"skip without first reversing the stocktake event"
            )
        diff.decision = ReconcileDiffDecisionEnum.SKIPPED.value
        diff.decided_by = approved_by
        diff.decided_at = datetime.now(UTC)
        await self._session.flush()
        log.info(
            "reconcile.diff_skipped",
            diff_id=diff.id,
            run_id=diff.reconcile_run_id,
            approved_by=approved_by,
        )
        return diff

    async def finalize_run(
        self,
        *,
        run_id: int,
        approved_by: str,
    ) -> ReconcileRun:
        """Transition a run from pending_approval -> applied (or cancelled).

        Does NOT trigger channel pushes; per D-6 those are batched and run
        by the caller (CLI / admin endpoint) after this returns. The caller
        can list approved diffs from `applied_count` / by filtering on the
        decision column.
        """
        run = await self._get_run(run_id)
        if run.status == ReconcileRunStatusEnum.APPLIED.value:
            return run
        if run.status == ReconcileRunStatusEnum.CANCELLED.value:
            raise RuntimeError(f"reconcile run id={run_id} was cancelled")

        # Count outstanding pending diffs — if any remain we don't allow
        # finalization (the operator needs to explicitly skip or approve).
        result = await self._session.execute(
            select(ReconcileDiff.id).where(
                ReconcileDiff.reconcile_run_id == run_id,
                ReconcileDiff.decision == ReconcileDiffDecisionEnum.PENDING.value,
            )
        )
        pending_ids = [r[0] for r in result.all()]
        if pending_ids:
            raise RuntimeError(
                f"reconcile run id={run_id} has {len(pending_ids)} pending "
                f"diffs; approve or skip them before finalizing"
            )

        run.status = ReconcileRunStatusEnum.APPLIED.value
        run.approved_by = approved_by
        run.approved_at = datetime.now(UTC)
        run.finished_at = datetime.now(UTC)
        await self._session.flush()
        log.info(
            "reconcile.run_finalized",
            run_id=run.id,
            applied_count=run.applied_count,
            approved_by=approved_by,
        )
        return run

    async def cancel_run(
        self,
        *,
        run_id: int,
        cancelled_by: str,
        reason: str | None = None,
    ) -> ReconcileRun:
        """Cancel a run before any approvals are applied. Cannot cancel a
        run that already has approved diffs."""
        run = await self._get_run(run_id)
        if run.status == ReconcileRunStatusEnum.APPLIED.value:
            raise RuntimeError(f"reconcile run id={run_id} is already applied")
        if (run.applied_count or 0) > 0:
            raise RuntimeError(
                f"reconcile run id={run_id} already has approved diffs; cannot cancel"
            )
        run.status = ReconcileRunStatusEnum.CANCELLED.value
        run.notes = reason
        run.approved_by = cancelled_by
        run.approved_at = datetime.now(UTC)
        run.finished_at = datetime.now(UTC)
        await self._session.flush()
        log.info(
            "reconcile.run_cancelled",
            run_id=run.id,
            cancelled_by=cancelled_by,
            reason=reason,
        )
        return run

    async def list_approved_diffs(self, run_id: int) -> list[ReconcileDiff]:
        """Return diffs that the operator approved for this run. Used by the
        F1.8 CLI / admin endpoint to drive post-approval channel pushes."""
        result = await self._session.execute(
            select(ReconcileDiff)
            .where(
                ReconcileDiff.reconcile_run_id == run_id,
                ReconcileDiff.decision == ReconcileDiffDecisionEnum.APPROVED.value,
            )
            .order_by(ReconcileDiff.id)
        )
        return list(result.scalars().all())

    # ---------- helpers ----------

    async def _get_diff(self, diff_id: int) -> ReconcileDiff:
        result = await self._session.execute(
            select(ReconcileDiff).where(ReconcileDiff.id == diff_id).with_for_update()
        )
        diff = result.scalar_one_or_none()
        if diff is None:
            raise ValueError(f"reconcile diff id={diff_id} not found")
        return diff

    async def _get_run(self, run_id: int) -> ReconcileRun:
        result = await self._session.execute(
            select(ReconcileRun).where(ReconcileRun.id == run_id).with_for_update()
        )
        run = result.scalar_one_or_none()
        if run is None:
            raise ValueError(f"reconcile run id={run_id} not found")
        return run

    async def _lock_or_create_snapshot(
        self,
        master_sku_id: int,
    ) -> InventorySnapshot:
        result = await self._session.execute(
            select(InventorySnapshot)
            .where(InventorySnapshot.master_sku_id == master_sku_id)
            .with_for_update()
        )
        snapshot = result.scalar_one_or_none()
        if snapshot is not None:
            return snapshot
        snapshot = InventorySnapshot(master_sku_id=master_sku_id, on_hand_qty=0)
        self._session.add(snapshot)
        await self._session.flush()
        return snapshot
