"""Read-only: what does a batched Shopify stock-audit query actually cost?

P2-035 replaces the daily CROSS MALL reconciliation with one against Shopify's
own inventory. The obvious shape — page through `productVariants` pulling each
variant's `inventoryLevel` inline — was flagged during review as likely to be
rejected outright: Shopify prices a query by its calculated cost, and a nested
connection multiplies. The existing adapter sidesteps this by reading on-hand
one variant at a time (`_ON_HAND_QUERY`), which always works but costs ~700
round trips per sweep on this catalogue.

Which shape to build on is a measurable question, and Shopify answers it itself:
every response carries `extensions.cost` with the requested cost, the actual
cost, and the current throttle bucket. So this probes descending page sizes and
reports what the API says, rather than us estimating from the documentation.

A rejection is a RESULT here, not a failure — it is the answer for that page
size — so the cost error is caught and reported alongside the sizes that worked.

Reads Shopify. Writes nothing, to Shopify or to the database.

Usage (Shopify credentials required):
    powershell -File scripts/run_cli.ps1 -Cli inspect_shopify_audit_cost -WithShopify
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from typing import Any

from app.adapters.shopify import STOCK_LEVELS_QUERY as AUDIT_QUERY
from app.cli._adapters import build_shopify_adapter
from app.logging import configure_logging, get_logger

log = get_logger(__name__)

#: The batched shape P2-035 would use: sku plus on-hand, in one round trip per
#: page. `inventoryLevel` nested inside the variant connection is the part whose
#: cost is in question.

PAGE_SIZES = (250, 100, 50, 25)

#: `_PRIMARY_LOCATION_QUERY` asks for `locations(first: 1)`, so every stock read
#: in this system reports ONE location. That is correct for a single-warehouse
#: shop and silently partial for any other — which would make our totals look
#: systematically larger than Shopify's by whatever the other locations hold.
ALL_LOCATIONS_QUERY = """
query AllLocations {
  locations(first: 50, query: "status:active") {
    edges { node { id name shipsInventory } }
  }
}
"""


async def probe(adapter: Any, *, location_id: str, first: int) -> dict[str, Any]:
    started = time.monotonic()
    try:
        body = await adapter._graphql(  # private on purpose: a probe, with no public equivalent
            AUDIT_QUERY, {"first": first, "cursor": None, "locId": location_id}
        )
    except Exception as exc:  # a cost rejection is a RESULT here, not a crash
        return {"first": first, "ok": False, "error": str(exc)[:220]}

    elapsed = time.monotonic() - started
    cost = (body.get("extensions") or {}).get("cost") or {}
    throttle = cost.get("throttleStatus") or {}
    edges = ((body.get("data") or {}).get("productVariants") or {}).get("edges") or []
    with_level = sum(
        1 for e in edges if ((e.get("node") or {}).get("inventoryItem") or {}).get("inventoryLevel")
    )
    return {
        "first": first,
        "ok": True,
        "returned": len(edges),
        "with_stock": with_level,
        "requested_cost": cost.get("requestedQueryCost"),
        "actual_cost": cost.get("actualQueryCost"),
        "available": throttle.get("currentlyAvailable"),
        "maximum": throttle.get("maximumAvailable"),
        "restore_rate": throttle.get("restoreRate"),
        "seconds": round(elapsed, 2),
    }


async def run(*, variants: int = 700) -> int:
    adapter = build_shopify_adapter(purpose="audit stock")
    try:
        location_id = await adapter._resolve_location_id()  # private: probe only
        body = await adapter._graphql(ALL_LOCATIONS_QUERY, {})
        locations = [
            e.get("node") or {}
            for e in (((body.get("data") or {}).get("locations") or {}).get("edges") or [])
        ]
        log.info("shopify.locations", count=len(locations), reading=location_id)
        results = []
        for first in PAGE_SIZES:
            outcome = await probe(adapter, location_id=location_id, first=first)
            results.append(outcome)
            log.info("shopify.audit_cost", **outcome)
            if outcome["ok"]:
                # The largest page size that works is the one to build on;
                # smaller ones only cost more round trips.
                break
    finally:
        close = getattr(adapter, "aclose", None) or getattr(adapter, "close", None)
        if close:
            await close()

    print("\n--- Shopify 在庫突合クエリのコスト実測 ---")
    print(f"  読み取り対象ロケーション: {location_id}")
    print(f"  有効なロケーション: {len(locations)}件")
    for loc in locations:
        mark = " <= 読み取り対象" if loc.get("id") == location_id else ""
        ships = "出荷可" if loc.get("shipsInventory") else "出荷不可"
        print(f"    {loc.get('name', '?'):24} {ships}  {loc.get('id', '')}{mark}")
    if len(locations) > 1:
        print(
            "\n  ※ 複数ロケーションあり。在庫読み取りは1拠点のみのため、"
            "他拠点の在庫は突合に含まれていない"
        )
    for r in results:
        if not r["ok"]:
            print(f"\n  first={r['first']:<4} 拒否")
            print(f"    {r['error']}")
            continue
        print(f"\n  first={r['first']:<4} 成功  ({r['seconds']}秒)")
        print(f"    返却        : {r['returned']} 件 (在庫取得済 {r['with_stock']} 件)")
        print(f"    コスト      : 要求 {r['requested_cost']} / 実際 {r['actual_cost']}")
        print(f"    バケット    : {r['available']} / {r['maximum']}  復元 {r['restore_rate']}/秒")
        actual, rate = r["actual_cost"], r["restore_rate"]
        if actual and rate and r["returned"]:
            pages = -(-variants // r["returned"])
            print(f"    全{variants}件の掃引: {pages} ページ / 約 {pages * actual / rate:.1f} 秒")

    good = [r for r in results if r["ok"]]
    print()
    if good:
        best = good[0]
        print(f"  => first={best['first']} で成立。1ページあたり実コスト {best['actual_cost']}。")
        print("     現行の1件ずつ取得 (約700往復) より大幅に少ない往復数で掃引できる。")
    else:
        print("  => いずれのページサイズも拒否された。1件ずつ取得する現行方式を維持する。")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Measure the Shopify stock-audit query cost")
    p.add_argument("--variants", type=int, default=700, help="Catalogue size, for extrapolation.")
    args = p.parse_args()
    configure_logging("INFO")
    sys.exit(asyncio.run(run(variants=args.variants)))


if __name__ == "__main__":
    main()
