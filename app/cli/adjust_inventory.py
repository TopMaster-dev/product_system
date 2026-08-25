"""Forward-fix inventory adjustment CLI (Phase 1-B).

Appends a compensating ``manual_adjust`` InventoryEvent AND updates the
snapshot atomically, via :meth:`InventoryService.manual_adjust`. This is the
operator-facing tool for the forward-fix rollback pattern — never UPDATE a
snapshot or DELETE events directly.

Primary use: reversing a reconcile verification approval (docs/13 §F1.7 /
§F1.8 cleanup). If approving a diff applied a stocktake of ``+D`` to a SKU,
run this with ``--delta -D`` to append a compensating event and restore the
snapshot, keeping the event log as the auditable source of truth.

Usage (via cloud-sql-proxy). Identify the master by id OR by sku_code — the
code is what an operator has in front of them, and a numeric id copied from a
URL is how the wrong SKU gets adjusted:

    py -m app.cli.adjust_inventory \\
        --sku-code B09 \\
        --delta -2 \\
        --reason "cutover後のキャンセル戻りによる幽霊在庫" \\
        --operator admin \\
        [--dry-run]

Exit codes:
  0 ok | 1 failed (sku not found / would go negative) | 2 usage error
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import MasterSku
from app.services.exceptions import InventoryInsufficientError, MasterSkuNotFoundError
from app.services.inventory import InventoryService

log = get_logger(__name__)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2

SessionFactory = async_sessionmaker[AsyncSession]


async def resolve_master_sku_id(factory: SessionFactory, sku_code: str) -> int | None:
    """sku_code -> master_sku_id. `sku_code` is UNIQUE, so this is unambiguous."""
    async with factory() as session:
        found: int | None = await session.scalar(
            select(MasterSku.id).where(MasterSku.sku_code == sku_code)
        )
    return found


async def run(
    *,
    master_sku_id: int | None = None,
    sku_code: str | None = None,
    delta: int,
    reason: str,
    operator: str,
    dry_run: bool = False,
    session_factory: SessionFactory | None = None,
) -> int:
    """Append a manual_adjust event of `delta` to one master.

    Identify the master by `master_sku_id` OR `sku_code`. The code is what an
    operator actually has in front of them, and typing a numeric id copied from
    a URL is how the wrong SKU gets adjusted — particularly here, where legacy
    masters sit alongside variants with confusingly similar names (`B09` and
    `B09gold`).

    `session_factory` defaults to the production `async_session_factory`;
    tests pass a factory bound to the test engine.
    """
    factory = session_factory or async_session_factory
    if delta == 0:
        sys.stderr.write("error: --delta must be non-zero\n")
        return EXIT_USAGE

    if master_sku_id is None:
        if not sku_code:
            sys.stderr.write("error: pass --master-sku-id or --sku-code\n")
            return EXIT_USAGE
        resolved = await resolve_master_sku_id(factory, sku_code)
        if resolved is None:
            sys.stderr.write(f"error: sku_code not found: {sku_code}\n")
            return EXIT_FAILED
        master_sku_id = resolved

    if dry_run:
        async with factory() as session:
            before = await InventoryService(session).get_current_stock(master_sku_id)
        projected = before + delta
        sys.stdout.write(
            json.dumps(
                {
                    "master_sku_id": master_sku_id,
                    "sku_code": sku_code,
                    "delta": delta,
                    "on_hand_before": before,
                    "on_hand_projected": projected,
                    "applied": False,
                    "would_go_negative": projected < 0,
                }
            )
            + "\n"
        )
        return EXIT_OK

    async with factory() as session, session.begin():
        svc = InventoryService(session)
        before = await svc.get_current_stock(master_sku_id)
        try:
            event = await svc.manual_adjust(
                master_sku_id=master_sku_id,
                quantity_delta=delta,
                reason=reason,
                operator=operator,
            )
        except (MasterSkuNotFoundError, InventoryInsufficientError, ValueError) as exc:
            log.warning(
                "adjust_inventory.failed",
                master_sku_id=master_sku_id,
                delta=delta,
                error=str(exc),
            )
            sys.stdout.write(
                json.dumps(
                    {
                        "master_sku_id": master_sku_id,
                        "delta": delta,
                        "result": "error",
                        "error": str(exc),
                    }
                )
                + "\n"
            )
            return EXIT_FAILED
        after = await svc.get_current_stock(master_sku_id)
        event_id = event.id

    sys.stdout.write(
        json.dumps(
            {
                "master_sku_id": master_sku_id,
                "sku_code": sku_code,
                "delta": delta,
                "event_id": event_id,
                "on_hand_before": before,
                "on_hand_after": after,
                "result": "applied",
            }
        )
        + "\n"
    )
    return EXIT_OK


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="adjust_inventory",
        description="Append a compensating manual_adjust event + update snapshot (forward-fix).",
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--master-sku-id", type=int)
    target.add_argument("--sku-code", help="Alternative to --master-sku-id; sku_code is UNIQUE.")
    parser.add_argument(
        "--delta",
        required=True,
        type=int,
        help="quantity_delta to apply (may be negative). Must be non-zero.",
    )
    parser.add_argument("--reason", required=True, help="Audit reason recorded on the event.")
    parser.add_argument("--operator", required=True, help="Who is performing the adjustment.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report current and projected on-hand without writing.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    configure_logging("INFO")
    try:
        args = parse_args(argv)
    except SystemExit as exc:
        return exc.code if isinstance(exc.code, int) else EXIT_USAGE
    return asyncio.run(
        run(
            master_sku_id=args.master_sku_id,
            sku_code=args.sku_code,
            delta=args.delta,
            reason=args.reason,
            operator=args.operator,
            dry_run=args.dry_run,
        )
    )


if __name__ == "__main__":
    sys.exit(main())
