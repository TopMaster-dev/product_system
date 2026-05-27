"""RakutenAdapter — RMS Order API (polling).

Phase 1-A scope:
- `fetch_orders`: searchOrder + getOrder against Rakuten ペイ注文API.
- Webhook is not provided by Rakuten — `verify_webhook` is a stub returning True.
- `push_inventory`: NOT IMPLEMENTED — lands in Phase 1-B.

Authentication is ESA (Encrypted Service Auth):
    Authorization: ESA <base64(serviceSecret:licenseKey)>
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

import httpx
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from app.adapters.base import (
    ChannelAdapter,
    NormalizedOrder,
    NormalizedOrderLine,
    NormalizedStatus,
)
from app.adapters.rate_limit import TokenBucket
from app.logging import get_logger

log = get_logger(__name__)

JST = timezone(timedelta(hours=9))

_BASE_URL = "https://api.rms.rakuten.co.jp/es/2.0/order"
_SEARCH_PATH = "/searchOrder/"
_GET_PATH = "/getOrder/"
# RakutenPay Order API response schema version. v7 is current as of 2024+.
_RAKUTEN_PAY_VERSION = 7

# Rakuten status code -> normalized status (subset; expanded as needed).
_STATUS_MAP: dict[int, NormalizedStatus] = {
    100: "pending",  # 注文確認待ち
    200: "confirmed",  # 楽天処理中
    300: "confirmed",  # 発送待ち
    400: "shipped",  # 変更確定待ち / 発送済
    500: "delivered",  # 発送完了
    600: "cancelled",  # payment in progress / later treated as cancellation
    700: "cancelled",  # キャンセル確定待ち
    800: "cancelled",  # キャンセル確定
    900: "returned",
}


class RakutenAdapter(ChannelAdapter):
    channel = "rakuten"

    def __init__(
        self,
        *,
        service_secret: str,
        license_key: str,
        shop_url: str | None = None,
        client: httpx.AsyncClient | None = None,
        rate_limiter: TokenBucket | None = None,
        base_url: str = _BASE_URL,
    ) -> None:
        if not service_secret or not license_key:
            raise ValueError("service_secret and license_key are required")
        # Strip whitespace defensively — Secret Manager values copied from
        # Windows sources can carry trailing \r, which corrupts the base64
        # auth token and triggers ES04-01 Bad Request on every call.
        self._service_secret = service_secret.strip()
        self._license_key = license_key.strip()
        self._shop_url = shop_url
        self._base_url = base_url
        self._client = client
        self._owns_client = client is None
        # Rakuten RMS API: 1 req/s sustained is conservative; bursts up to 5.
        self._rate_limiter = rate_limiter or TokenBucket(rate=1, capacity=5)

    async def __aenter__(self) -> RakutenAdapter:
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
            self._owns_client = True
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    # ---------- ChannelAdapter API ----------

    async def fetch_orders(
        self,
        since: datetime,
        until: datetime | None = None,
    ) -> list[NormalizedOrder]:
        order_numbers = await self._search_order_numbers(since, until)
        if not order_numbers:
            return []
        out: list[NormalizedOrder] = []
        # Rakuten getOrder supports up to 100 order numbers per call.
        for batch in _batched(order_numbers, 100):
            details = await self._get_orders(batch)
            for raw in details:
                out.append(self._to_normalized(raw))
        return out

    async def push_inventory(self, sku: str, quantity: int) -> None:
        raise NotImplementedError("RakutenAdapter.push_inventory is Phase 1-B")

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool:
        # Rakuten does not deliver webhooks for orders; polling is authoritative.
        return True

    # ---------- helpers ----------

    def _auth_header(self) -> dict[str, str]:
        token = base64.b64encode(f"{self._service_secret}:{self._license_key}".encode()).decode(
            "ascii"
        )
        return {
            "Authorization": f"ESA {token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError,)),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        await self._rate_limiter.acquire()
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
            self._owns_client = True
        resp = await self._client.post(
            self._base_url + path,
            json=body,
            headers=self._auth_header(),
        )
        if resp.status_code >= 400:
            log.error(
                "rakuten.api_error",
                status=resp.status_code,
                path=path,
                body_preview=resp.text[:500],
            )
        resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data

    async def _search_order_numbers(
        self,
        since: datetime,
        until: datetime | None,
    ) -> list[str]:
        # RakutenPay Order API searchOrder. dateType 1=注文日, 4=注文確定日, etc.
        # We use 1 (注文日) — every order has one, and incremental ingest catches
        # everything updated since the last poll regardless of fulfilment state.
        # All timestamps MUST be in JST per Rakuten spec.
        end = until or datetime.now(tz=UTC)
        # PaginationRequestModel and orderProgressList are REQUIRED by Rakuten
        # — omitting either returns ES04-01 Bad Request. requestRecordsAmount
        # default is 30; we use 1000 (the max) to minimize pagination passes.
        body: dict[str, Any] = {
            "dateType": 1,
            "startDatetime": _fmt_jst(since),
            "endDatetime": _fmt_jst(end),
            "orderProgressList": [100, 200, 300, 400, 500, 600, 700, 800, 900],
            "PaginationRequestModel": {
                "requestRecordsAmount": 1000,
                "requestPage": 1,
            },
        }
        data = await self._post(_SEARCH_PATH, body)
        numbers = data.get("orderNumberList") or []
        return [str(n) for n in numbers]

    async def _get_orders(self, order_numbers: list[str]) -> list[dict[str, Any]]:
        data = await self._post(
            _GET_PATH,
            {"orderNumberList": order_numbers, "version": _RAKUTEN_PAY_VERSION},
        )
        return list(data.get("OrderModelList") or [])

    @staticmethod
    def _to_normalized(raw: dict[str, Any]) -> NormalizedOrder:
        status = _STATUS_MAP.get(int(raw.get("orderProgress", 0)), "confirmed")
        items: list[NormalizedOrderLine] = []
        for pkg in raw.get("PackageModelList") or []:
            for ln in pkg.get("ItemModelList") or []:
                items.append(
                    NormalizedOrderLine(
                        line_id=str(ln.get("itemDetailId") or ln.get("itemNumber")),
                        channel_sku=str(ln.get("manageNumber") or ln.get("itemNumber") or ""),
                        channel_product_id=str(ln.get("itemNumber") or ""),
                        quantity=int(ln.get("units") or 0),
                        unit_price=Decimal(str(ln.get("price") or "0")),
                        currency="JPY",
                    )
                )
        ordered_at = datetime.fromisoformat(
            (raw.get("orderDatetime") or raw.get("ordDatetime") or "").replace("Z", "+00:00")
        )
        return NormalizedOrder(
            channel="rakuten",
            channel_order_id=str(raw["orderNumber"]),
            status=status,
            ordered_at=ordered_at,
            items=items,
            raw_payload=raw,
        )


def _batched(seq: list[str], size: int) -> list[list[str]]:
    return [seq[i : i + size] for i in range(0, len(seq), size)]


def _fmt_jst(dt: datetime) -> str:
    """Format a datetime in Rakuten's expected `YYYY-MM-DDTHH:MM:SS+0900` shape."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(JST).strftime("%Y-%m-%dT%H:%M:%S+0900")
