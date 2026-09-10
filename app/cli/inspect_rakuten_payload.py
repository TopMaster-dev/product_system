"""Read-only: does a stored Rakuten order payload carry SKU-level identity?

The adapter lifts only `manageNumber` out of an order line
(app/adapters/rakuten.py), but it stores the WHOLE payload in
`orders.raw_payload`. So if 楽天 returns a SKU-level identifier for
項目選択肢別在庫 items, it is already in our database for every order we have
ever ingested — and the 137 unresolved 商品管理番号 become a reprocessing job
over data we hold, rather than something only fixable for future orders.

That difference is worth a quote's worth of money, so it should be measured
rather than assumed. This reports which keys actually appear on order lines and
how often, which is the first question P2-038 (仕様調査) has to answer.

PRIVACY: a 楽天 order payload contains the purchaser's name, address and phone
number. This prints KEY NAMES only, never values — except for the handful of
identifier fields named in SKU_KEYS, which are product codes, not personal data.
Nothing here is written anywhere.

Usage (via the Cloud SQL proxy):
    py -m app.cli.inspect_rakuten_payload
    py -m app.cli.inspect_rakuten_payload --limit 500
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import sys
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import Order

log = get_logger(__name__)
SessionFactory = async_sessionmaker[AsyncSession]

CHANNEL = "rakuten"

#: Identifier fields whose VALUES are safe to print — product codes, not people.
#: `SkuModelList` is the nested structure 楽天 documents for 項目選択肢別在庫.
SKU_KEYS = (
    "SkuModelList",
    "skuModelList",
    "variantId",
    "skuId",
    "merchantDefinedSkuId",
    "skuManageNumber",
    "SKU管理番号",
)


def _lines(payload: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for pkg in payload.get("PackageModelList") or []:
        for line in pkg.get("ItemModelList") or []:
            if isinstance(line, dict):
                out.append(line)
    return out


async def collect(session: AsyncSession, *, limit: int) -> dict[str, Any]:
    rows = (
        await session.execute(
            select(Order.raw_payload)
            .where(Order.channel == CHANNEL, Order.raw_payload.is_not(None))
            .order_by(desc(Order.ordered_at))
            .limit(limit)
        )
    ).all()

    keys: collections.Counter[str] = collections.Counter()
    sku_hits: collections.Counter[str] = collections.Counter()
    samples: list[str] = []
    orders = lines = 0

    for (payload,) in rows:
        if not isinstance(payload, dict):
            continue
        orders += 1
        for line in _lines(payload):
            lines += 1
            keys.update(line.keys())
            for key in SKU_KEYS:
                value = line.get(key)
                if value in (None, "", [], {}):
                    continue
                sku_hits[key] += 1
                if len(samples) < 5:
                    if isinstance(value, list) and value and isinstance(value[0], dict):
                        samples.append(f"{key}[0] keys: {sorted(value[0].keys())}")
                    else:
                        samples.append(f"{key} = {value!r}")
    return {
        "orders": orders,
        "lines": lines,
        "line_keys": keys,
        "sku_hits": sku_hits,
        "samples": samples,
    }


async def run(*, limit: int = 200, session_factory: SessionFactory | None = None) -> int:
    factory = session_factory or async_session_factory
    async with factory() as session:
        found = await collect(session, limit=limit)

    log.info(
        "rakuten.payload_shape",
        orders=found["orders"],
        lines=found["lines"],
        sku_fields=dict(found["sku_hits"]),
    )
    print(f"\n--- 楽天受注ペイロードの構造 (直近{found['orders']}件 / 明細{found['lines']}行) ---")
    if not found["orders"]:
        print("  楽天の受注が保存されていません")
        return 0

    print("\n  明細行に現れるキー:")
    for key, count in found["line_keys"].most_common():
        mark = " <= SKU候補" if key in SKU_KEYS else ""
        print(f"    {key:28} {count:6}{mark}")

    print("\n  SKU単位の識別子:")
    if found["sku_hits"]:
        for key, count in found["sku_hits"].most_common():
            print(f"    {key:28} {count:6} / {found['lines']} 行")
        print("\n  例:")
        for sample in found["samples"]:
            print(f"    {sample}")
        print("\n  => 保存済みペイロードにSKU識別子あり。過去分も遡って解決できる見込み。")
    else:
        print("    なし")
        print("\n  => 保存済みペイロードにSKU識別子なし。受注APIの取得項目の見直しが必要。")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="Inspect stored Rakuten order payload shape")
    p.add_argument("--limit", type=int, default=200, help="How many recent orders to sample.")
    args = p.parse_args()
    configure_logging("INFO")
    sys.exit(asyncio.run(run(limit=args.limit)))


if __name__ == "__main__":
    main()
