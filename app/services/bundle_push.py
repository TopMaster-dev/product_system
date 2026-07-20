"""BundlePushService — batched push of derived bundle/shared-stock availability.

Per client decision D-6, channel pushes are BATCHED (post-reconcile / scheduled),
not fired inside order ingestion. For each bundle/shared-stock parent this
computes get_bundle_available (max(0, min over components)) and pushes it via
InventoryPushService. A component that just changed (e.g. shared N23) fans out to
every dependent bundle via `dependent_bundle_ids`.

The service does NOT commit; the caller owns the transaction.
"""

from __future__ import annotations

from collections.abc import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import ChannelAdapter
from app.logging import get_logger
from app.models import BundleComponent, ChannelSkuMapping, MasterSku, SyncAttempt
from app.notifications.slack import SlackNotifier
from app.services.inventory import InventoryService
from app.services.inventory_push import InventoryPushService, PushRequest

log = get_logger(__name__)


class BundlePushService:
    def __init__(self, session: AsyncSession, notifier: SlackNotifier | None = None) -> None:
        self._session = session
        self._inventory = InventoryService(session)
        self._push = InventoryPushService(session, notifier)

    async def all_bundle_ids(self) -> list[int]:
        """Every is_bundle master (set parents + shared-stock brackets)."""
        result = await self._session.execute(
            select(MasterSku.id).where(MasterSku.is_bundle.is_(True))
        )
        return list(result.scalars().all())

    async def dependent_bundle_ids(self, component_ids: Iterable[int]) -> list[int]:
        """The distinct bundles that consume any of these components — the set to
        re-derive & re-push when a component's stock changes (shared N23 -> its 4
        sets; an anklet -> its bracelet)."""
        ids = list(component_ids)
        if not ids:
            return []
        result = await self._session.execute(
            select(BundleComponent.bundle_master_sku_id)
            .where(BundleComponent.component_master_sku_id.in_(ids))
            .distinct()
        )
        return list(result.scalars().all())

    async def _channel_sku(self, bundle_id: int, channel: str) -> str | None:
        result = await self._session.execute(
            select(ChannelSkuMapping.channel_sku).where(
                ChannelSkuMapping.master_sku_id == bundle_id,
                ChannelSkuMapping.channel == channel,
                ChannelSkuMapping.is_active.is_(True),
            )
        )
        return result.scalars().first()

    async def push_bundles(
        self,
        adapter: ChannelAdapter,
        bundle_ids: Iterable[int],
        *,
        triggered_by: str,
        dry_run: bool = False,
    ) -> list[SyncAttempt]:
        """Compute each bundle's derived availability and push it to the adapter's
        channel. Bundles with no active mapping on that channel are skipped."""
        attempts: list[SyncAttempt] = []
        for bundle_id in bundle_ids:
            channel_sku = await self._channel_sku(bundle_id, adapter.channel)
            if not channel_sku:
                log.info(
                    "bundle_push.skip_no_mapping",
                    bundle_master_sku_id=bundle_id,
                    channel=adapter.channel,
                )
                continue
            available = await self._inventory.get_bundle_available(bundle_id)
            if dry_run:
                log.info(
                    "bundle_push.dry_run",
                    bundle_master_sku_id=bundle_id,
                    channel_sku=channel_sku,
                    available=available,
                )
                continue
            attempt = await self._push.push_single(
                adapter,
                PushRequest(
                    master_sku_id=bundle_id,
                    channel_sku=channel_sku,
                    quantity=available,
                    triggered_by=triggered_by,
                ),
            )
            attempts.append(attempt)
        return attempts
