"""Batched bundle/shared-stock availability push (Phase 1-B, D-6).

Computes each bundle/shared-stock parent's derived availability
(max(0, min over components)) and pushes it to Shopify. Intended to run
POST-reconcile / on a schedule (Cloud Scheduler), not inside order ingestion —
consistent with D-6 (pushes are batched, not sale-triggered).

--dry-run computes + logs each availability without pushing.

Usage (Cloud Run Job):
    py -m app.cli.push_bundle_availability [--dry-run] [--triggered-by cloud_scheduler]
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from app.cli._adapters import build_shopify_adapter
from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import SyncAttemptStatusEnum
from app.notifications.slack import get_slack_notifier
from app.services import BundlePushService

log = get_logger(__name__)


async def run(*, dry_run: bool, triggered_by: str) -> int:
    adapter = build_shopify_adapter(purpose="push")
    notifier = get_slack_notifier()
    async with adapter, async_session_factory() as session, session.begin():
        svc = BundlePushService(session, notifier)
        bundle_ids = await svc.all_bundle_ids()
        attempts = await svc.push_bundles(
            adapter, bundle_ids, triggered_by=triggered_by, dry_run=dry_run
        )
        n_bundles, n_pushed = len(bundle_ids), len(attempts)
        n_failed = sum(1 for a in attempts if a.status == SyncAttemptStatusEnum.FAILED.value)
    log.info(
        "bundle_push.done",
        bundles=n_bundles,
        pushed=n_pushed,
        failed=n_failed,
        dry_run=dry_run,
    )
    return 1 if n_failed else 0


def main() -> None:
    p = argparse.ArgumentParser(description="Batched bundle availability push to Shopify")
    p.add_argument("--triggered-by", default="cloud_scheduler", dest="triggered_by")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()
    configure_logging("INFO")
    sys.exit(asyncio.run(run(dry_run=args.dry_run, triggered_by=args.triggered_by)))


if __name__ == "__main__":
    main()
