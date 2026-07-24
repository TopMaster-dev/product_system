"""ChannelAdapter abstract base class — common interface for all channels.

Phase 1-A implements `fetch_orders` and `verify_webhook`.
`push_inventory` is implemented in Phase 1-B (writeback).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field

ChannelName = Literal["rakuten", "shopify", "amazon", "wholesale"]
NormalizedStatus = Literal[
    "pending",  # order received, not yet processed
    "confirmed",  # confirmed by channel
    "shipped",
    "delivered",
    "cancelled",
    "returned",
]


class NormalizedOrderLine(BaseModel):
    """Channel-agnostic order line item."""

    line_id: str
    channel_sku: str
    channel_product_id: str | None = None
    product_name: str | None = None  # channel's own product/item name (for alerts)
    quantity: int = Field(gt=0)
    unit_price: Decimal
    currency: str = "JPY"
    fulfillment_type: str | None = None  # FBA / MFN / self / None


class NormalizedOrder(BaseModel):
    """Channel-agnostic order representation.

    Each ChannelAdapter is responsible for converting channel-specific
    payloads into this shape so downstream services stay channel-neutral.
    """

    channel: ChannelName
    channel_order_id: str
    marketplace_id: str | None = None
    status: NormalizedStatus
    ordered_at: datetime
    items: list[NormalizedOrderLine]
    raw_payload: dict[str, Any] | None = None


class ChannelAdapter(ABC):
    """Abstract interface every channel must implement."""

    channel: ChannelName

    @abstractmethod
    async def fetch_orders(
        self,
        since: datetime,
        until: datetime | None = None,
    ) -> list[NormalizedOrder]:
        """Fetch orders updated within the given window."""

    @abstractmethod
    async def push_inventory(
        self,
        sku: str,
        quantity: int,
    ) -> dict[str, Any] | None:
        """Push inventory level to the channel.

        Implementations should return the channel API response as a dict on
        success (so the caller can persist it in `sync_attempts.response_payload`)
        or `None` if the channel does not provide a structured response.
        Phase 1-A stubs were ``None``-returning; Phase 1-B implementations
        (Rakuten F1.5, Shopify F1.6) return the full response payload.
        """

    @abstractmethod
    def verify_webhook(self, headers: dict[str, str], body: bytes) -> bool:
        """Verify webhook authenticity (HMAC, etc.).

        Channels without webhooks may return True unconditionally.
        """
