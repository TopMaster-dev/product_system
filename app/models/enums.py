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
    IN_PROGRESS = "in_progress"
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


class SyncAttemptTypeEnum(StrEnum):
    PUSH_INVENTORY = "push_inventory"
    RECONCILE = "reconcile"


class SyncAttemptStatusEnum(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class ReconcileRunStatusEnum(StrEnum):
    RUNNING = "running"
    PENDING_APPROVAL = "pending_approval"
    APPLIED = "applied"
    CANCELLED = "cancelled"


class ReconcileDiffDecisionEnum(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    SKIPPED = "skipped"


class NonInventoryKindEnum(StrEnum):
    """Why a master SKU carries no physical stock.

    Set together with `master_skus.is_stock_managed = false`; a CHECK constraint
    keeps the two in lockstep. Such SKUs never produce inventory events, so they
    cannot accumulate the runaway negatives seen in Phase 1-B (gift boxes reached
    -12509), and they are excluded from analytics and reorder.
    """

    PACKAGING = "packaging"  # ギフトボックス・ラッピング材
    COUPON = "coupon"  # クーポン・値引き行
    MADE_TO_ORDER = "made_to_order"  # 受注生産・長さ変更等
    SERVICE = "service"  # 役務・手数料
    OTHER = "other"
