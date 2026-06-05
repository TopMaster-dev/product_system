"""One-off: clean up Shopify orders duplicated by the gid/numeric ID bug.

Background: until v0.2.2, the polling path stored channel_order_id as
`gid://shopify/Order/<n>` while the webhook path stored just `<n>`. The same
physical order ended up as two distinct rows, and the (event_type,
source_channel, source_order_id, source_line_id) UNIQUE on inventory_events
could not block the duplicate consumption events because source_order_id
differed between the two paths.

This CLI:
  1. Finds every gid-format Shopify order whose numeric twin also exists.
  2. Deletes the gid-format Order (cascades to order_items).
  3. Deletes the inventory_events keyed off the gid-format source_order_id.
  4. Rebuilds on_hand_qty for each affected master_sku from the remaining
     events (sum of quantity_delta), keeping the snapshot internally consistent.

Idempotent — re-running after a clean state is a no-op.

Usage:
    py -m app.cli.dedupe_shopify_orders [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import text

from app.db import async_session_factory
from app.logging import configure_logging, get_logger

log = get_logger(__name__)


async def run(*, dry_run: bool = False) -> int:
    log.info("dedupe.start", dry_run=dry_run)

    async with async_session_factory() as session, session.begin():
        # Step 1: find duplicate pairs.
        pairs_result = await session.execute(text("""
            SELECT g.id AS gid_pk, g.channel_order_id AS gid_id,
                   n.id AS num_pk, n.channel_order_id AS num_id
            FROM orders g
            JOIN orders n ON n.channel = 'shopify'
              AND n.channel_order_id = regexp_replace(g.channel_order_id, '^gid://shopify/Order/', '')
            WHERE g.channel = 'shopify'
              AND g.channel_order_id LIKE 'gid://shopify/Order/%'
        """))
        pairs = pairs_result.all()
        log.info("dedupe.pairs_found", count=len(pairs))
        gid_ids = [p.gid_id for p in pairs]
        gid_pks = [p.gid_pk for p in pairs]

        # Step 2: identify the duplicate inventory_events tied to those gids.
        events_result = await session.execute(
            text("""
                SELECT id, master_sku_id, quantity_delta, source_order_id, source_line_id
                FROM inventory_events
                WHERE event_type = 'order_consumed'
                  AND source_channel = 'shopify'
                  AND source_order_id = ANY(:gid_ids)
            """),
            {"gid_ids": gid_ids},
        )
        events = events_result.all()
        affected_skus = {e.master_sku_id for e in events}
        log.info("dedupe.events_to_remove",
                 count=len(events),
                 affected_master_skus=len(affected_skus),
                 total_quantity_delta=sum(e.quantity_delta for e in events))

        # Items that will cascade-delete with the orders (just for reporting).
        items_count = await session.execute(
            text("SELECT COUNT(*) FROM order_items WHERE order_id = ANY(:pks)"),
            {"pks": gid_pks},
        )
        log.info("dedupe.order_items_cascade", count=items_count.scalar())

        if dry_run:
            log.info("dedupe.dry_run_complete")
            return 0

        if not pairs:
            log.info("dedupe.no_pair_deletes_needed")

        # Step 3a: delete inventory_events tied to gid-format source_order_id.
        # No-op when pairs is empty.
        del_events = await session.execute(
            text("""
                DELETE FROM inventory_events
                WHERE event_type = 'order_consumed'
                  AND source_channel = 'shopify'
                  AND source_order_id = ANY(:gid_ids)
            """),
            {"gid_ids": gid_ids},
        )
        log.info("dedupe.events_deleted", rowcount=del_events.rowcount)

        # Step 3b: delete the gid-format orders (cascades order_items).
        del_orders = await session.execute(
            text("""
                DELETE FROM orders
                WHERE id = ANY(:pks)
            """),
            {"pks": gid_pks},
        )
        log.info("dedupe.orders_deleted", rowcount=del_orders.rowcount)

        # Step 4: rebuild on_hand_qty for affected snapshots from remaining
        # events. This is more thorough than a delta-correction: it guarantees
        # the snapshot matches SUM(events) regardless of any other drift.
        if affected_skus:
            recomputed = await session.execute(
                text("""
                    UPDATE inventory_snapshots s
                    SET on_hand_qty = COALESCE(t.total, 0),
                        updated_at = NOW()
                    FROM (
                        SELECT master_sku_id, SUM(quantity_delta)::int AS total
                        FROM inventory_events
                        WHERE master_sku_id = ANY(:sku_ids)
                        GROUP BY master_sku_id
                    ) t
                    WHERE s.master_sku_id = t.master_sku_id
                """),
                {"sku_ids": list(affected_skus)},
            )
            log.info("dedupe.snapshots_recomputed", rowcount=recomputed.rowcount)

    # Step 5: canonicalize the remaining solo gid-format orders to numeric
    # format, so the next polling cycle (which now writes numeric IDs via
    # v0.2.2's _strip_gid) finds them via the (channel, channel_order_id)
    # UNIQUE rather than creating a fresh duplicate.
    async with async_session_factory() as session, session.begin():
        # orders.channel_order_id
        res_o = await session.execute(text("""
            UPDATE orders
            SET channel_order_id = regexp_replace(channel_order_id, '^gid://shopify/Order/', '')
            WHERE channel = 'shopify'
              AND channel_order_id LIKE 'gid://shopify/Order/%'
        """))
        # order_items.line_id (stored on the rows whose order survived)
        res_i = await session.execute(text("""
            UPDATE order_items
            SET line_id = regexp_replace(line_id, '^gid://shopify/LineItem/', '')
            WHERE line_id LIKE 'gid://shopify/LineItem/%'
        """))
        # inventory_events.source_order_id / source_line_id
        res_e_o = await session.execute(text("""
            UPDATE inventory_events
            SET source_order_id = regexp_replace(source_order_id, '^gid://shopify/Order/', '')
            WHERE source_channel = 'shopify'
              AND source_order_id LIKE 'gid://shopify/Order/%'
        """))
        res_e_l = await session.execute(text("""
            UPDATE inventory_events
            SET source_line_id = regexp_replace(source_line_id, '^gid://shopify/LineItem/', '')
            WHERE source_channel = 'shopify'
              AND source_line_id LIKE 'gid://shopify/LineItem/%'
        """))
        log.info("dedupe.canonicalized",
                 orders=res_o.rowcount,
                 order_items=res_i.rowcount,
                 events_order_id=res_e_o.rowcount,
                 events_line_id=res_e_l.rowcount)

    log.info("dedupe.done")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Dedupe Shopify gid/numeric duplicate orders")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    configure_logging("INFO")
    sys.exit(asyncio.run(run(dry_run=args.dry_run)))


if __name__ == "__main__":
    main()
