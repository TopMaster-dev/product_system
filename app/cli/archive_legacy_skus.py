"""Archive the ~400 legacy product-level masters the variant cutover replaced.

After the cutover every real SKU is a variant master (`sku_code` = the Shopify
SKU) and the old product-level rows are inert: no active mapping, no stock, no
sales. They still occupy the inventory list though, where they make up the bulk
of the "ゼロ" badge and drown the rows an operator actually needs. Archiving is a
VISIBILITY change only — `archived_at` is never consulted by a period aggregate,
so historical sales stay intact (see `app.services.sku_scope`).

Scope is exactly `zero_legacy_stock.legacy_master_ids`: masters whose 商品コード
appears in a `channel='crossmall'` mapping (i.e. really migrated), excluding the
variant masters themselves. One scope definition, shared, so the two CLIs cannot
disagree about what "legacy" means.

Three guards refuse to archive anything still in use, because archiving a live
SKU hides it from the operator while orders keep consuming it:

* non-zero stock — someone still holds physical inventory under this master;
* an active channel mapping — a channel can still order it;
* it is a component of a non-archived bundle — the set would lose a component
  from view while still fanning out to it.

Reversible: ``--unarchive`` clears the flag for the same scope.

Usage (via the Cloud SQL proxy):
    py -m app.cli.archive_legacy_skus --dry-run
    py -m app.cli.archive_legacy_skus --reason "variant cutover 2026-07-20"
    py -m app.cli.archive_legacy_skus --unarchive
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.cli.zero_legacy_stock import legacy_master_ids
from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import MasterSku
from app.services.sku_scope import archive_blockers

log = get_logger(__name__)
SessionFactory = async_sessionmaker[AsyncSession]

DEFAULT_REASON = "variant cutover: legacy product-level master"


@dataclass(slots=True)
class ArchiveResult:
    in_scope: int = 0
    archived: int = 0
    already: int = 0
    skipped_stock: list[str] = field(default_factory=list)
    skipped_mapping: list[str] = field(default_factory=list)
    skipped_component: list[str] = field(default_factory=list)

    @property
    def skipped(self) -> int:
        return len(self.skipped_stock) + len(self.skipped_mapping) + len(self.skipped_component)


async def run(
    *,
    dry_run: bool,
    reason: str = DEFAULT_REASON,
    unarchive: bool = False,
    session_factory: SessionFactory | None = None,
) -> int:
    factory = session_factory or async_session_factory
    result = ArchiveResult()

    async with factory() as session, session.begin():
        ids = await legacy_master_ids(session)
        result.in_scope = len(ids)
        if not ids:
            log.info("archive_legacy.empty_scope")
            return 0

        masters = {
            m.id: m
            for m in (
                await session.execute(select(MasterSku).where(MasterSku.id.in_(ids)))
            ).scalars()
        }

        if unarchive:
            for master in masters.values():
                if master.archived_at is None:
                    result.already += 1
                    continue
                result.archived += 1
                if not dry_run:
                    master.archived_at = None
                    master.archived_reason = None
        else:
            blockers = await archive_blockers(session, ids)
            now = datetime.now(UTC)
            for mid, master in sorted(masters.items()):
                if master.archived_at is not None:
                    result.already += 1
                    continue
                # Report every reason a master is blocked, not just the first —
                # otherwise a second run "discovers" a new blocker each time and
                # the operator cannot tell how much work is really left.
                blocked = False
                if mid in blockers.with_stock:
                    # Carry the quantity: "4 skipped" is not reviewable, but
                    # "B09=-3" vs "B09=120" is the difference between running a
                    # stocktake and asking the client where the stock went.
                    result.skipped_stock.append(f"{master.sku_code}={blockers.stock_qty[mid]}")
                    blocked = True
                if mid in blockers.with_active_mapping:
                    result.skipped_mapping.append(master.sku_code)
                    blocked = True
                if mid in blockers.component_of_live_bundle:
                    result.skipped_component.append(master.sku_code)
                    blocked = True
                if blocked:
                    continue
                result.archived += 1
                if not dry_run:
                    master.archived_at = now
                    master.archived_reason = reason

        log.info(
            "archive_legacy.dry_run" if dry_run else "archive_legacy.done",
            mode="unarchive" if unarchive else "archive",
            in_scope=result.in_scope,
            affected=result.archived,
            already_in_state=result.already,
            skipped=result.skipped,
            skipped_stock=result.skipped_stock[:30],
            skipped_mapping=result.skipped_mapping[:30],
            skipped_component=result.skipped_component[:30],
        )
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Archive legacy product-level masters")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--reason", default=DEFAULT_REASON)
    p.add_argument("--unarchive", action="store_true", help="Clear archived_at for the scope.")
    args = p.parse_args()
    configure_logging("INFO")
    sys.exit(asyncio.run(run(dry_run=args.dry_run, reason=args.reason, unarchive=args.unarchive)))


if __name__ == "__main__":
    main()
