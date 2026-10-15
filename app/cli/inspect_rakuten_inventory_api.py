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

API_HOST = "https://api.rms.rakuten.co.jp/es"
ITEM_PATH = "/2.0/items/manage-numbers/{manage_number}"
INVENTORY_PATH = "/2.0/inventories/manage-numbers/{manage_number}/variants/{variant_id}"

#: How the RMS gateway answers, and what each answer proves. Established by probing
#: with a DELIBERATELY BOGUS token on 2026-10-12, so routing could be read without
#: any credential in play:
#:
#:   404 + server: Apache      the path is not an RMS route at all
#:   404 + GF0002 (istio)      the prefix is routed, this leaf is not an endpoint
#:   400 + GF0001 (istio)      real endpoint, bad path parameter
#:   400 + GK0001 (istio)      real endpoint, no Authorization header
#:   401 + GA0001 (istio)      real endpoint, credential presented and REFUSED
#:
#: Routing is resolved BEFORE the credential, so 401 can only mean "this endpoint
#: exists and your licence key may not use it". That is what makes the grant map
#: below readable: with the real key, 200 = granted, 401 = not granted.
NOT_A_ROUTE = "ルートなし (RMSのAPIではない)"
NOT_AN_ENDPOINT = "エンドポイントなし"
BAD_PATH_PARAM = "パスパラメータ誤り (エンドポイントは実在)"
NO_AUTH_HEADER = "Authorizationヘッダ欠落"
NOT_GRANTED = "未許可 (利用機能設定)"
GRANTED = "利用可能"
BAD_METHOD = "メソッド誤り"
BAD_BODY = "許可あり・リクエスト本文が不一致"


#: Enough of a 200 to learn the field names from, short enough to read.
BODY_PREVIEW = 150


def describe_body(body: dict[str, Any]) -> str:
    """Top-level keys, and the first element's keys when the answer is a list —
    that is where a per-SKU record's field names live."""
    parts: list[str] = []
    for key, value in list(body.items())[:6]:
        if isinstance(value, list):
            inner = value[0] if value and isinstance(value[0], dict) else None
            shape = "{" + ", ".join(list(inner)[:8]) + "}" if inner else f"[{len(value)}]"
            parts.append(f"{key}[{len(value)}]={shape}")
        elif isinstance(value, dict):
            parts.append(f"{key}{{{', '.join(list(value)[:6])}}}")
        else:
            parts.append(f"{key}={value}")
    return "; ".join(parts)[:BODY_PREVIEW]


def classify(status: int, server: str, code: str) -> str:
    """What one response proves about the endpoint and our licence."""
    if status == 200:
        return GRANTED
    if status == 404:
        return NOT_AN_ENDPOINT if "istio" in server.lower() else NOT_A_ROUTE
    if status == 401:
        return NOT_GRANTED
    if status == 405:
        return BAD_METHOD
    if status == 400:
        if code.startswith("GF0001"):
            return BAD_PATH_PARAM
        if code.startswith("GK0001"):
            return NO_AUTH_HEADER
        return BAD_BODY
    return f"HTTP {status}"


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
    """One call: its status, the `server` header, its JSON, Rakuten's error if any.

    `server` is not decoration — it is the only thing separating "not an RMS route"
    (Apache) from "routed, but not an endpoint" (istio-envoy).
    """

    status: int
    body: dict[str, Any] | None
    error: str = ""
    server: str = ""

    @property
    def ok(self) -> bool:
        return self.status == 200 and self.body is not None

    @property
    def meaning(self) -> str:
        return classify(self.status, self.server, self.error)

    @property
    def summary(self) -> str:
        """What to show beside the verdict: the error when there is one, and on
        success the SHAPE of the answer.

        The shape is the point once the permission is granted. Rakuten's own
        spec is behind a login, so the field names in a real 200 are how we
        learn what the stock write must send. Inventory responses carry stock
        numbers only — no purchaser data — so printing them is safe.
        """
        if self.error:
            return self.error[:46]
        return "" if self.body is None else describe_body(self.body)


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
    """Read-only RMS client. GET, plus POST for the two `bulk-get` endpoints —
    which are reads despite the verb. It can reach no endpoint that writes."""

    #: POST is allowed ONLY to these. `bulk-upsert` writes stock and is absent on
    #: purpose: a typo in a manage number there would zero a live product's stock.
    READ_ONLY_POSTS = frozenset({"/2.0/inventories/bulk-get", "/2.1/inventories/bulk-get"})

    def __init__(self, *, service_secret: str, license_key: str, client: httpx.AsyncClient):
        # .strip(): Windows-sourced secrets carry a trailing CR that corrupts
        # the base64 token (memory: Secret Manager CRLF).
        token = base64.b64encode(f"{service_secret.strip()}:{license_key.strip()}".encode()).decode(
            "ascii"
        )
        self._headers = {
            "Authorization": f"ESA {token}",
            "Content-Type": "application/json; charset=utf-8",
        }
        self._client = client
        self._first = True

    async def call(self, method: str, path: str, body: dict[str, Any] | None = None) -> Probe:
        if method == "POST" and path not in self.READ_ONLY_POSTS:
            raise ValueError(f"this probe may not POST to {path}")
        if not self._first:
            await asyncio.sleep(CALL_INTERVAL)
        self._first = False
        try:
            resp = await self._client.request(
                method, API_HOST + path, headers=self._headers, json=body, timeout=30.0
            )
        except httpx.HTTPError as exc:
            return Probe(status=0, body=None, error=f"{type(exc).__name__}: {exc}"[:200])
        try:
            parsed = resp.json()
        except ValueError:
            parsed = None
        error = "" if resp.status_code == 200 else rakuten_error(parsed) or resp.text[:120]
        return Probe(
            status=resp.status_code,
            body=parsed if isinstance(parsed, dict) else None,
            error=error,
            server=resp.headers.get("server", ""),
        )

    async def get(self, path: str) -> Probe:
        return await self.call("GET", path)

    async def item(self, manage_number: str) -> Probe:
        return await self.get(ITEM_PATH.format(manage_number=quote(manage_number, safe="")))

    async def inventory(self, manage_number: str, variant_id: str) -> Probe:
        return await self.get(
            INVENTORY_PATH.format(
                manage_number=quote(manage_number, safe=""), variant_id=quote(variant_id, safe="")
            )
        )


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


def verdict(statuses: list[int], *, baseline_ok: bool) -> str:
    """One line on whether this API is usable with our licence.

    `baseline_ok` is whether the item API answered 200 in THIS run with THESE
    credentials. It is what separates the two readings of a 401: with a working
    baseline the credentials are proven good, so a 401 is a permission the shop
    has not granted — not a key to go and check. The first version of this
    function said 「認証エラー — サービスシークレット/ライセンスキーを確認」 for
    every 401, which would have sent the client hunting a credential that is
    demonstrably fine (2026-10-12).
    """
    if not statuses:
        return "未確認"
    counts = Counter(statuses)
    if counts[200]:
        return f"{GRANTED} ({counts[200]}/{len(statuses)} 件で応答)"
    if counts[401]:
        return (
            f"{NOT_GRANTED} — 同一実行で商品APIは200のため、認証情報は正常。"
            "RMSの利用機能設定で、このAPIが有効になっていません"
            if baseline_ok
            else "401 — 商品APIも200ではないため、認証情報と権限のどちらが原因か切り分け不能"
        )
    if counts[400]:
        return f"{BAD_BODY} — リクエスト本文を要確認"
    if counts[404]:
        return "エンドポイントに到達できません"
    return f"応答なし (ステータス {dict(counts)})"


# --- 2. Which APIs may this licence key use? ---------------------------------------


#: The grant map's baseline: the one call already proven to work. Everything else
#: is read relative to it.
BASELINE_FAMILY = "商品API 2.0 取得"


@dataclass(frozen=True, slots=True)
class GrantCall:
    """One probe of the gateway. `family` groups calls into the grant map."""

    family: str
    method: str
    path: str
    body: dict[str, Any] | None = None
    #: A control establishes how the gateway behaves; it is not a grant question.
    control: str = ""


def grant_calls(manage_number: str, variant_id: str) -> list[GrantCall]:
    """Controls first, so the run proves its own discriminators before leaning
    on them, then one call per API family we might need.

    `bulk-get` is a POST that READS. `bulk-upsert`, the write, is deliberately
    absent: whether we may write is a separate question that needs the client's
    sign-off and a SKU chosen so a mistake cannot cost a live product its stock.
    """
    one = {"inventories": [{"manageNumber": manage_number, "variantId": variant_id}]}
    return [
        GrantCall(
            BASELINE_FAMILY,
            "GET",
            ITEM_PATH.format(manage_number=manage_number),
            control="基準: これが200でなければ以降は解釈不能",
        ),
        GrantCall(
            "(制御) 不正なパスパラメータ",
            "GET",
            ITEM_PATH.format(manage_number=manage_number) + "/no-such-leaf",
            control="期待: 400 GF0001 — 経路の検証が認証より先である証拠",
        ),
        GrantCall(
            "(制御) 未登録のエンドポイント",
            "GET",
            "/2.0/inventories/no-such-endpoint",
            control="期待: 404 GF0002 — 401は経路単位であり前方一致ではない証拠",
        ),
        GrantCall(
            "(制御) RMSの経路ではないURL",
            "GET",
            "/2.0/this/path/does/not/exist",
            control="期待: 404 (Apache) — 本番コードの送信先と同じ分類",
        ),
        GrantCall("商品API 2.0 検索", "GET", "/2.0/items/search"),
        GrantCall(
            "在庫API 2.0 単体取得",
            "GET",
            INVENTORY_PATH.format(manage_number=manage_number, variant_id=variant_id),
        ),
        GrantCall("在庫API 2.0 一括取得", "POST", "/2.0/inventories/bulk-get", one),
        GrantCall(
            "在庫API 2.0 範囲取得",
            "GET",
            "/2.0/inventories/bulk-get/range?minQuantity=0&maxQuantity=1",
        ),
        GrantCall(
            "在庫API 2.1 単体取得",
            "GET",
            f"/2.1/inventories/manage-numbers/{manage_number}/variants/{variant_id}",
        ),
        GrantCall("在庫API 2.1 一括取得", "POST", "/2.1/inventories/bulk-get", one),
    ]


def scope_branch(grants: dict[str, Probe]) -> str:
    """Which of the three explanations the grant map is consistent with."""
    baseline = grants.get(BASELINE_FAMILY)
    item_ok = baseline is not None and baseline.ok
    inventory = [p for f, p in grants.items() if f.startswith("在庫API")]
    if not item_ok:
        return "判定不能 — 基準の商品APIが200ではありません"
    if any(p.ok for p in inventory):
        return (
            "在庫APIの読み取りは既に利用可能です。"
            "クライアントへの依頼は不要か、書き込みのみに縮小できます"
        )
    if all(p.status == 401 for p in inventory if p.status):
        return (
            "認証情報は正常で、在庫APIだけが未許可です。"
            "原因は (a) 利用機能が未設定 (b) 設定後にキーを再発行していない (c) 在庫APIが未契約 "
            "のいずれかで、画面を見ないと区別できません"
        )
    return "混在した応答です。下の表をそのまま共有してください"


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
    grants: dict[str, Probe] = {}
    async with httpx.AsyncClient() as client:
        rms = RmsReader(
            service_secret=settings.rakuten_service_secret,
            license_key=settings.rakuten_license_key,
            client=client,
        )

        print("\n  2. このライセンスキーで使えるAPI (読み取りのみ)")
        for call in grant_calls(picked[0].manage_number, picked[0].variant_id):
            probe = await rms.call(call.method, call.path, call.body)
            grants[call.family] = probe
            print(f"     {call.family:<26}{probe.meaning:<30}{probe.status:<6}{probe.summary}")
            if call.control:
                print(f"       {call.control}")
            if call.control.startswith("基準") and not probe.ok:
                print(
                    "\n     ★ 基準の商品APIが200を返しません。"
                    "認証情報そのものを先に確認してください。"
                )
                return 1

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

    baseline_ok = any(s == 200 for s in item_status)
    print("\n  4. 判定")
    print(f"     商品API 2.0 : {verdict(item_status, baseline_ok=baseline_ok)}")
    print(f"     在庫API 2.0 : {verdict(inv_status, baseline_ok=baseline_ok)}")
    if baseline_ok:
        answered = sum(s == 200 for s in item_status)
        print(f"     受注のSKUが商品のSKUに見つかった: {matched}/{answered}")
    print(f"\n     {scope_branch(grants)}")

    log.info(
        "rakuten_inventory_api.done",
        samples=len(picked),
        item_status=dict(Counter(item_status)),
        inventory_status=dict(Counter(inv_status)),
        matched=matched,
        grants={family: probe.status for family, probe in grants.items()},
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
