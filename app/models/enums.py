"""Domain enums — stored as short strings to keep migrations Postgres-portable."""

from __future__ import annotations

from enum import StrEnum


class ChannelEnum(StrEnum):
    RAKUTEN = "rakuten"
    SHOPIFY = "shopify"
    AMAZON = "amazon"
    WHOLESALE = "wholesale"


class OrderStatusEnum(StrEnum):
    PENDING_MAPPING = "pending_mapping"
    CONFIRMED = "confirmed"
    SHIPPED = "shipped"
    DELIVERED = "delivered"
    CANCELLED = "cancelled"
    RETURNED = "returned"


class InventoryEventTypeEnum(StrEnum):
    ORDER_CONSUMED = "order_consumed"
    CANCELLATION_RETURNED = "cancellation_returned"
    MANUAL_ADJUST = "manual_adjust"
    STOCKTAKE = "stocktake"
    RECEIPT = "receipt"


class MappingAlertStatusEnum(StrEnum):
    OPEN = "open"
    RESOLVED = "resolved"
    IGNORED = "ignored"


class WebhookStatusEnum(StrEnum):
    RECEIVED = "received"
    PROCESSED = "processed"
    REJECTED = "rejected"
    FAILED = "failed"


class FulfillmentTypeEnum(StrEnum):
    SELF = "self"
    FBA = "fba"
    MFN = "mfn"
