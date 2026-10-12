"""Read-only: can we read Rakuten's per-SKU stock, and do our IDs match? (P2-038)

The shop's whole Rakuten catalogue is 項目選択肢別在庫: stock is held per SKU,
not per product. The current push (`RakutenAdapter.push_inventory`) sends one
product-level figure with no SKU identifier, so it cannot set this shop's stock
at all. Before building the SKU-level push, three facts have to be established
against the real API rather than assumed:

1. **The item API answers** and lists each product's SKUs (`variants`) with the
   IDs the push will need — and whether our license may call it.
2. **The inventory API answers** for one product + SKU — again, permission first:
   a license without 在庫API rights fails here, not in the push.
3. **The IDs in our orders are the IDs Rakuten lists.** Every stored order line
   carries `SkuModelList[0].variantId` and `merchantDefinedSkuId`. If the
   `variantId` we hold is a key of the item's `variants`, orders can be resolved
   and stock pushed against the same identifier.

READ ONLY: GET requests only, at most one per second. Nothing is written to
Rakuten or to our database. Order payloads are read for product-identity
fields only (manageNumber, variantId, merchantDefinedSkuId) — never a
purchaser's name, address or phone.

    powershell -File scripts/run_cli.ps1 -Cli inspect_rakuten_inventory_api -WithRakuten
    powershell -File scripts/run_cli.ps1 -Cli inspect_rakuten_inventory_api -WithRakuten ^
        -Args "--samples 10"
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import sys
from collections import Counter
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import async_session_factory
from app.logging import configure_logging, get_logger
from app.models import Order

log = get_logger(__name__)

API_ROOT = "https://api.rms.rakuten.co.jp/es/2.0"
ITEM_PATH = "/items/manage-numbers/{manage_number}"
INVENTORY_PATH = "/inventories/manage-numbers/{manage_number}/variants/{variant_id}"
SEARCH_PATH = "/items/search?hits=1"

#: Recent Rakuten orders scanned for SKU references.
ORDER_SCAN_LIMIT = 500
#: Seconds between API calls — RMS allows about one per second.
CALL_INTERVAL = 1.1


# --- 1. What our orders say ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VariantRef:
    manage_number: str
    variant_id: str
    merchant_sku_id: str


@dataclass(slots=True)
class OrderRefs:
    lines: int = 0
    with_variant_id: int = 0
    with_merchant_sku_id: int = 0
    refs: dict[tuple[str, str], VariantRef] = field(default_factory=dict)

    def add_payload(self, payload: dict[str, Any]) -> None:
        """Reads manageNumber and SkuModelList only — never the purchaser."""
        for pkg in payload.get("PackageModelList") or []:
            for line in pkg.get("ItemModelList") or []:
                self.lines += 1
                manage_number = str(line.get("manageNumber") or "").strip()
                models = line.get("SkuModelList") or line.get("skuModelList") or []
                first = models[0] if models and isinstance(models[0], dict) else {}
                variant_id = str(first.get("variantId") or "").strip()
                merchant = str(first.get("merchantDefinedSkuId") or "").strip()
                self.with_variant_id += bool(variant_id)
                self.with_merchant_sku_id += bool(merchant)
                # Either ID can name the SKU; keep the line if it has one.
                if manage_number and (variant_id or merchant):
                    self.refs.setdefault(
                        (manage_number, variant_id or f"#{merchant}"),
                        VariantRef(manage_number, variant_id, merchant),
                    )

    def sample(self, n: int) -> list[VariantRef]:
        """Spread over products first: one SKU per product until `n` are
        picked, then fill from the rest."""
        refs = list(self.refs.values())
        first_per_product: dict[str, VariantRef] = {}
        for r in refs:
            first_per_product.setdefault(r.manage_number, r)
        picked = list(first_per_product.values())[:n]
        seen = set(picked)
        picked += [r for r in refs if r not in seen][: n - len(picked)]
        return picked


async def collect_order_refs(session: AsyncSession, *, limit: int = ORDER_SCAN_LIMIT) -> OrderRefs:
    refs = OrderRefs()
    rows = await session.execute(
        select(Order.raw_payload)
        .where(Order.channel == "rakuten", Order.raw_payload.is_not(None))
        .order_by(Order.ordered_at.desc())
        .limit(limit)
    )
    for (payload,) in rows.all():
        if isinstance(payload, dict):
            refs.add_payload(payload)
    return refs


# --- 2. What Rakuten says --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Probe:
    """One GET: its status, its JSON when there is any, Rakuten's error if any."""

    status: int
    body: dict[str, Any] | None
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.status == 200 and self.body is not None


def rakuten_error(body: Any) -> str:
    """`errors: [{code, message}]` in v2; tolerate the older shapes too."""
    if not isinstance(body, dict):
        return ""
    errors = body.get("errors") or body.get("Errors") or []
    if isinstance(errors, dict):
        errors = [errors]
    parts = [
        f"{e.get('code', '')} {e.get('message', '')}".strip() for e in errors if isinstance(e, dict)
    ]
    if not parts and body.get("errorCode"):
        parts = [f"{body.get('errorCode')} {body.get('message', '')}".strip()]
    return "; ".join(parts)[:200]


class RmsReader:
    """GET-only client for the RMS v2 item and inventory APIs."""

    def __init__(self, *, service_secret: str, license_key: str, client: httpx.AsyncClient):
        # .strip(): Windows-sourced secrets carry a trailing CR that corrupts
        # the base64 token (memory: Secret Manager CRLF).
        token = base64.b64encode(f"{service_secret.strip()}:{license_key.strip()}".encode()).decode(
            "ascii"
        )
        self._headers = {"Authorization": f"ESA {token}"}
        self._client = client
        self._first = True

    async def get(self, path: str) -> Probe:
        if not self._first:
            await asyncio.sleep(CALL_INTERVAL)
        self._first = False
        try:
            resp = await self._client.get(API_ROOT + path, headers=self._headers, timeout=30.0)
        except httpx.HTTPError as exc:
            return Probe(status=0, body=None, error=f"{type(exc).__name__}: {exc}"[:200])
        try:
            body = resp.json()
        except ValueError:
            body = None
        error = "" if resp.status_code == 200 else rakuten_error(body) or resp.text[:200]
        return Probe(
            status=resp.status_code, body=body if isinstance(body, dict) else None, error=error
        )

    async def item(self, manage_number: str) -> Probe:
        return await self.get(ITEM_PATH.format(manage_number=quote(manage_number, safe="")))

    async def inventory(self, manage_number: str, variant_id: str) -> Probe:
        return await self.get(
            INVENTORY_PATH.format(
                manage_number=quote(manage_number, safe=""), variant_id=quote(variant_id, safe="")
            )
        )

    async def search(self) -> Probe:
        return await self.get(SEARCH_PATH)


@dataclass(frozen=True, slots=True)
class ItemView:
    """What the item API says about one product, as the push needs it."""

    variant_count: int
    #: The SKU the order line refers to, as the item API names it, or "".
    variant_id: str
    #: How it was found: by the order's variantId, or by merchantDefinedSkuId.
    matched_by: str
    merchant_sku_id: str
    variant_keys: tuple[str, ...]


def read_item(body: dict[str, Any], ref: VariantRef) -> ItemView:
    variants = body.get("variants") or {}
    if isinstance(variants, list):  # tolerate a list-shaped variant collection
        variants = {str(v.get("variantId", i)): v for i, v in enumerate(variants)}
    if not isinstance(variants, dict):
        variants = {}
    matched_by, key = "", ""
    if ref.variant_id and isinstance(variants.get(ref.variant_id), dict):
        matched_by, key = "variantId", ref.variant_id
    elif ref.merchant_sku_id:
        for vid, v in variants.items():
            merchant = str(v.get("merchantDefinedSkuId") or "") if isinstance(v, dict) else ""
            if merchant == ref.merchant_sku_id:
                matched_by, key = "merchantDefinedSkuId", str(vid)
                break
    mine = variants.get(key) if key else {}
    mine = mine if isinstance(mine, dict) else {}
    return ItemView(
        variant_count=len(variants),
        variant_id=key,
        matched_by=matched_by,
        merchant_sku_id=str(mine.get("merchantDefinedSkuId") or ""),
        variant_keys=tuple(sorted(mine.keys())),
    )


def verdict(statuses: list[int]) -> str:
    """One line on whether this API is usable with our license."""
    if not statuses:
        return "未確認"
    counts = Counter(statuses)
    if counts[200]:
        return f"利用可能 ({counts[200]}/{len(statuses)} 件で応答)"
    if counts[401]:
        return "認証エラー — サービスシークレット/ライセンスキーを確認"
    if counts[403]:
        return "権限なし — RMS の「WEB API サービス」設定でこのAPIの利用を有効にする必要があります"
    if counts[404] == len(statuses):
        return "404 のみ — エンドポイントの誤り、または対象が存在しない (エラー内容を確認)"
    return f"応答なし (ステータス {dict(counts)})"


@dataclass(frozen=True, slots=True)
class SampleResult:
    line: str
    item_status: int
    #: None when the SKU could not be identified, so the inventory API was not asked.
    inventory_status: int | None
    matched: bool
    variant_keys: tuple[str, ...]


async def probe_sample(rms: RmsReader, ref: VariantRef) -> SampleResult:
    """Item API for the product, then the inventory API for the SKU — using the
    SKU id the item API confirmed, so a line known only by its
    merchantDefinedSkuId is still checked."""
    item = await rms.item(ref.manage_number)
    line = f"     {ref.manage_number:<16}{ref.variant_id or '—':<22}"
    sku_id = ref.variant_id
    view: ItemView | None = None
    if item.ok and item.body is not None:
        view = read_item(item.body, ref)
        sku_id = view.variant_id or sku_id
        found = f"あり ({view.matched_by})" if view.variant_id else "★なし"
        merchant = view.merchant_sku_id or ref.merchant_sku_id or "—"
        line += f" 商品API: SKU {view.variant_count}件中に{found} merchantDefinedSkuId={merchant}"
    else:
        line += f" 商品API: HTTP {item.status} {item.error}"

    inventory_status: int | None = None
    if not sku_id:
        line += " / 在庫API: SKU を特定できないため未確認"
    else:
        inv = await rms.inventory(ref.manage_number, sku_id)
        inventory_status = inv.status
        if inv.ok and inv.body is not None:
            line += f" / 在庫API: 在庫 {inv.body.get('quantity', '?')}"
        else:
            line += f" / 在庫API: HTTP {inv.status} {inv.error}"

    return SampleResult(
        line=line,
        item_status=item.status,
        inventory_status=inventory_status,
        matched=bool(view and view.variant_id),
        variant_keys=view.variant_keys if view else (),
    )


# --- run ---------------------------------------------------------------------------


async def run(*, samples: int, scan: int) -> int:
    settings = get_settings()
    if not settings.rakuten_service_secret or not settings.rakuten_license_key:
        print("  楽天の認証情報がありません。-WithRakuten を付けて実行してください。")
        return 2

    async with async_session_factory() as session:
        refs = await collect_order_refs(session, limit=scan)

    print("\n  === P2-038 楽天 項目選択肢別在庫の確認 — 読み取りのみ ===")
    print(f"\n  1. 受注データ (直近{scan}注文)")
    print(
        f"     明細 {refs.lines}件 / variantId あり {refs.with_variant_id}件"
        f" / merchantDefinedSkuId あり {refs.with_merchant_sku_id}件"
    )
    print(f"     異なる 商品管理番号とSKU の組み合わせ {len(refs.refs)}件")
    picked = refs.sample(samples)
    if not picked:
        print("     ★ 確認に使えるSKU情報がありません")
        return 1

    item_status: list[int] = []
    inv_status: list[int] = []
    matched = 0
    first_keys: tuple[str, ...] = ()
    async with httpx.AsyncClient() as client:
        rms = RmsReader(
            service_secret=settings.rakuten_service_secret,
            license_key=settings.rakuten_license_key,
            client=client,
        )
        search = await rms.search()
        print(f"\n  2. 商品API 2.0 検索: HTTP {search.status} {search.error}")
        if search.ok and search.body is not None:
            print(f"     登録商品数 numFound={search.body.get('numFound')}")

        print(f"\n  3. サンプル {len(picked)}件")
        for ref in picked:
            result = await probe_sample(rms, ref)
            item_status.append(result.item_status)
            if result.inventory_status is not None:
                inv_status.append(result.inventory_status)
            matched += result.matched
            first_keys = first_keys or result.variant_keys
            print(result.line)

        if first_keys:
            print(f"\n     参考: SKU の項目 {', '.join(first_keys)}")

    print("\n  4. 判定")
    print(f"     商品API 2.0 : {verdict(item_status)}")
    print(f"     在庫API 2.0 : {verdict(inv_status)}")
    if any(s == 200 for s in item_status):
        answered = sum(s == 200 for s in item_status)
        print(f"     受注のSKUが商品のSKUに見つかった: {matched}/{answered}")

    log.info(
        "rakuten_inventory_api.done",
        samples=len(picked),
        item_status=dict(Counter(item_status)),
        inventory_status=dict(Counter(inv_status)),
        matched=matched,
    )
    return 0


def main() -> None:
    p = argparse.ArgumentParser(
        description="Read-only probe of the Rakuten item and inventory APIs"
    )
    p.add_argument("--samples", type=int, default=5, help="product+SKU pairs to look up")
    p.add_argument("--scan", type=int, default=ORDER_SCAN_LIMIT, help="recent orders to scan")
    args = p.parse_args()
    configure_logging("INFO")
    sys.exit(asyncio.run(run(samples=max(1, min(args.samples, 30)), scan=args.scan)))


if __name__ == "__main__":
    main()
