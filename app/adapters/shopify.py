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

# Phase 1-B F1.6: inventory writeback.
# Discovers the primary location when SHOPIFY_LOCATION_ID is not configured.
_PRIMARY_LOCATION_QUERY = """
query PrimaryLocation {
  locations(first: 1, query: "status:active") {
    edges { node { id name } }
  }
}
"""

# Looks up an inventoryItemId from a SKU. SKUs are unique per shop in
# Shopify; if more than one is returned we treat it as ambiguous and raise.
_INVENTORY_ITEM_BY_SKU_QUERY = """
query InventoryItemBySku($q: String!) {
  inventoryItems(first: 2, query: $q) {
    edges { node { id sku } }
  }
}
"""

# Absolute SET (matches Rakuten's updateInventory operation 1). The central
# DB is authoritative, so SET avoids any drift from increment/decrement
# off a stale baseline.
_INVENTORY_SET_MUTATION = """
mutation InventorySet($input: InventorySetOnHandQuantitiesInput!) {
  inventorySetOnHandQuantities(input: $input) {
    inventoryAdjustmentGroup { id createdAt }
    userErrors { field message code }
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
        location_id: str = "",
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
        # Cached location GID for the inventory writeback. Empty = auto-discover
        # on first push (per client decision D-2: single-location shops).
        self._location_id = location_id

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

    async def push_inventory(self, sku: str, quantity: int) -> dict[str, Any] | None:
        """Set the on-hand inventory for `sku` at the shop's primary location.

        Per client decision D-1 we use an absolute SET (matching Rakuten's
        updateInventory operation 1); the central DB is authoritative.

        Per D-2 we auto-discover the location on first call when no explicit
        location_id was supplied to the adapter, then cache it for subsequent
        pushes in the same process.

        Raises on logical failures (SKU not found, multiple SKUs match,
        Shopify userErrors) so the InventoryPushService logs the failure to
        sync_attempts.
        """
        if quantity < 0:
            raise ValueError(
                "Shopify rejects negative on-hand quantities; callers must clamp at 0 before push"
            )
        location_id = await self._resolve_location_id()
        inventory_item_id = await self._lookup_inventory_item_id(sku)
        body = await self._graphql(
            _INVENTORY_SET_MUTATION,
            {
                "input": {
                    "reason": "correction",
                    "setQuantities": [
                        {
                            "inventoryItemId": inventory_item_id,
                            "locationId": location_id,
                            "quantity": int(quantity),
                        }
                    ],
                }
            },
        )
        result = (body.get("data") or {}).get("inventorySetOnHandQuantities") or {}
        user_errors = result.get("userErrors") or []
        if user_errors:
            raise RuntimeError(f"Shopify inventory set rejected for sku={sku}: {user_errors}")
        return result

    async def _resolve_location_id(self) -> str:
        if self._location_id:
            return self._location_id
        body = await self._graphql(_PRIMARY_LOCATION_QUERY, {})
        edges = ((body.get("data") or {}).get("locations") or {}).get("edges") or []
        if not edges:
            raise RuntimeError(
                "Shopify primary location auto-discovery returned no active "
                "locations; configure SHOPIFY_LOCATION_ID explicitly"
            )
        loc_id = (edges[0].get("node") or {}).get("id")
        if not loc_id:
            raise RuntimeError("Shopify primary location node missing `id` field")
        self._location_id = str(loc_id)
        return self._location_id

    async def _lookup_inventory_item_id(self, sku: str) -> str:
        if not sku:
            raise ValueError("SKU is required for Shopify inventory push")
        # Shopify query syntax: `sku:<value>`. Escape any embedded quotes.
        safe = sku.replace('"', '\\"')
        body = await self._graphql(
            _INVENTORY_ITEM_BY_SKU_QUERY,
            {"q": f'sku:"{safe}"'},
        )
        edges = ((body.get("data") or {}).get("inventoryItems") or {}).get("edges") or []
        if not edges:
            raise RuntimeError(f"Shopify inventory item not found for sku={sku!r}")
        if len(edges) > 1:
            # `first: 2` above is the canary; if we get back two rows the SKU
            # is ambiguous and we should not pick one arbitrarily.
            raise RuntimeError(
                f"Shopify returned multiple inventory items for sku={sku!r}; "
                f"refusing to set quantity ambiguously"
            )
        item_id = (edges[0].get("node") or {}).get("id")
        if not item_id:
            raise RuntimeError(f"Shopify inventoryItems node missing `id` for sku={sku!r}")
        return str(item_id)

    @retry(
        retry=retry_if_exception_type((httpx.HTTPError,)),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        stop=stop_after_attempt(5),
        reraise=True,
    )
    async def _graphql(self, query: str, variables: dict[str, Any]) -> dict[str, Any]:
        """Execute a GraphQL operation with the same auth, rate-limit, and
        retry as the orders fetch path."""
        await self._rate_limiter.acquire()
        if self._client is None:
            self._client = httpx.AsyncClient(timeout=30.0)
            self._owns_client = True
        resp = await self._client.post(
            self._endpoint,
            json={"query": query, "variables": variables},
            headers={
                "X-Shopify-Access-Token": self._access_token,
                "Content-Type": "application/json",
            },
        )
        if resp.status_code >= 400:
            log.error(
                "shopify.api_error",
                status=resp.status_code,
                body_preview=resp.text[:500],
            )
        resp.raise_for_status()
        body: dict[str, Any] = resp.json()
        if body.get("errors"):
            raise RuntimeError(f"Shopify GraphQL errors: {body['errors']}")
        return body

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
