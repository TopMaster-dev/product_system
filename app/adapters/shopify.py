"""ShopifyAdapter — Admin GraphQL API.

Phase 1-A scope:
- `fetch_orders`: paginated GraphQL query against `orders` connection.
- `verify_webhook`: HMAC-SHA256 of raw request body using webhook shared secret.
- `push_inventory`: NOT IMPLEMENTED — lands in Phase 1-B.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from datetime import datetime
from decimal import Decimal
from typing import Any, ClassVar

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


_ORDERS_QUERY = """
query Orders($first: Int!, $query: String!, $cursor: String) {
  orders(first: $first, query: $query, after: $cursor, sortKey: UPDATED_AT) {
    pageInfo { hasNextPage endCursor }
    edges {
      node {
        id
        name
        cancelledAt
        displayFulfillmentStatus
        displayFinancialStatus
        createdAt
        updatedAt
        lineItems(first: 50) {
          edges {
            node {
              id
              sku
              quantity
              variant { id product { id } }
              originalUnitPriceSet { shopMoney { amount currencyCode } }
            }
          }
        }
      }
    }
  }
}
"""


def _strip_gid(value: str | None) -> str:
    """Normalize Shopify GraphQL global IDs to the numeric suffix.

    Webhook payloads carry numeric IDs ("6772971667500"); GraphQL responses
    carry `gid://shopify/<Type>/<num>`. Storing both formats verbatim breaks
    the (channel, channel_order_id) UNIQUE that's supposed to make the
    Webhook+Polling redundancy idempotent. We canonicalize on the numeric form.
    """
    if value is None:
        return ""
    s = str(value)
    if "/" in s:
        return s.rsplit("/", 1)[-1]
    return s


def _map_status(node: dict[str, Any]) -> NormalizedStatus:
    if node.get("cancelledAt"):
        return "cancelled"
    fulfillment = (node.get("displayFulfillmentStatus") or "").upper()
    if fulfillment in {"FULFILLED"}:
        return "shipped"
    if fulfillment in {"PARTIALLY_FULFILLED", "IN_PROGRESS"}:
        return "confirmed"
    return "confirmed"


class ShopifyAdapter(ChannelAdapter):
    channel = "shopify"
    HEADER_HMAC: ClassVar[str] = "x-shopify-hmac-sha256"
    HEADER_WEBHOOK_ID: ClassVar[str] = "x-shopify-webhook-id"
    HEADER_TOPIC: ClassVar[str] = "x-shopify-topic"

    def __init__(
        self,
        *,
        shop_domain: str,
        access_token: str,
        webhook_secret: str,
        api_version: str = "2025-04",
        client: httpx.AsyncClient | None = None,
        rate_limiter: TokenBucket | None = None,
    ) -> None:
        if not shop_domain:
            raise ValueError("shop_domain is required")
        self._shop_domain = shop_domain
        self._access_token = access_token
        self._webhook_secret = webhook_secret
        self._endpoint = f"https://{shop_domain}/admin/api/{api_version}/graphql.json"
        self._client = client
        self._owns_client = client is None
        # Shopify GraphQL costs are calculated points; 50/s sustained is safe.
        self._rate_limiter = rate_limiter or TokenBucket(rate=50, capacity=100)

    async def __aenter__(self) -> ShopifyAdapter:
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
        query = self._build_search_query(since, until)
        cursor: str | None = None
        out: list[NormalizedOrder] = []
        while True:
            page = await self._fetch_page(query=query, cursor=cursor)
            for edge in page.get("edges", []):
                out.append(self._to_normalized(edge["node"]))
            info = page.get("pageInfo") or {}
            if not info.get("hasNextPage"):
                break
            cursor = info.get("endCursor")
            if cursor is None:
                break
        return out

    async def push_inventory(self, sku: str, quantity: int) -> None:
        raise NotImplementedError("ShopifyAdapter.push_inventory is Phase 1-B")

    def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool:
        received = self._header(headers, self.HEADER_HMAC)
        if not received:
            return False
        digest = hmac.new(
            self._webhook_secret.encode("utf-8"),
            body,
            hashlib.sha256,
        ).digest()
        expected = base64.b64encode(digest).decode("ascii")
        return hmac.compare_digest(received, expected)

    # ---------- helpers ----------

    @staticmethod
    def _header(headers: dict[str, str], name: str) -> str | None:
        lower = name.lower()
        for k, v in headers.items():
            if k.lower() == lower:
                return v
        return None

    def _build_search_query(self, since: datetime, until: datetime | None) -> str:
        q = f"updated_at:>={since.isoformat()}"
        if until is not None:
            q += f" updated_at:<={until.isoformat()}"
        return q

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError,)),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    async def _fetch_page(self, *, query: str, cursor: str | None) -> dict[str, Any]:
        await self._rate_limiter.acquire()
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
            self._owns_client = True

        payload = {
            "query": _ORDERS_QUERY,
            "variables": {"first": 100, "query": query, "cursor": cursor},
        }
        resp = await self._client.post(
            self._endpoint,
            json=payload,
            headers={
                "X-Shopify-Access-Token": self._access_token,
                "Content-Type": "application/json",
            },
        )
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        if body.get("errors"):
            raise RuntimeError(f"Shopify GraphQL errors: {body['errors']}")
        orders: dict[str, Any] = body["data"]["orders"]
        return orders

    @staticmethod
    def normalize_webhook_order(payload: dict[str, Any]) -> NormalizedOrder:
        """Convert a Shopify REST-shaped webhook body to NormalizedOrder."""
        items: list[NormalizedOrderLine] = []
        for ln in payload.get("line_items") or []:
            price = ln.get("price") or "0"
            currency = (
                (ln.get("price_set") or {}).get("shop_money", {}).get("currency_code")
                or payload.get("currency")
                or "JPY"
            )
            items.append(
                NormalizedOrderLine(
                    line_id=str(ln["id"]),
                    channel_sku=ln.get("sku") or "",
                    channel_product_id=str(ln.get("variant_id") or ""),
                    quantity=int(ln.get("quantity") or 0),
                    unit_price=Decimal(str(price)),
                    currency=currency,
                )
            )
        cancelled_at = payload.get("cancelled_at")
        fulfillment = (payload.get("fulfillment_status") or "").lower()
        if cancelled_at:
            status: NormalizedStatus = "cancelled"
        elif fulfillment == "fulfilled":
            status = "shipped"
        elif fulfillment in {"partial", "in_progress"}:
            status = "confirmed"
        else:
            status = "confirmed"
        ordered_at = datetime.fromisoformat(
            (payload.get("created_at") or "").replace("Z", "+00:00")
        )
        return NormalizedOrder(
            channel="shopify",
            channel_order_id=str(payload["id"]),
            status=status,
            ordered_at=ordered_at,
            items=items,
            raw_payload=payload,
        )

    @staticmethod
    def _to_normalized(node: dict[str, Any]) -> NormalizedOrder:
        items: list[NormalizedOrderLine] = []
        for edge in (node.get("lineItems") or {}).get("edges", []):
            ln = edge["node"]
            money = (ln.get("originalUnitPriceSet") or {}).get("shopMoney") or {}
            items.append(
                NormalizedOrderLine(
                    line_id=_strip_gid(ln["id"]),
                    channel_sku=ln.get("sku") or "",
                    channel_product_id=_strip_gid((ln.get("variant") or {}).get("id")),
                    quantity=int(ln["quantity"]),
                    unit_price=Decimal(str(money.get("amount", "0"))),
                    currency=money.get("currencyCode", "JPY"),
                )
            )
        ordered_at = datetime.fromisoformat(node["createdAt"].replace("Z", "+00:00"))
        return NormalizedOrder(
            channel="shopify",
            channel_order_id=_strip_gid(node["id"]),
            status=_map_status(node),
            ordered_at=ordered_at,
            items=items,
            raw_payload=node,
        )
