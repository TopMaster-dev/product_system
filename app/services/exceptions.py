"""Domain exceptions raised by services."""

from __future__ import annotations


class ServiceError(Exception):
    """Base class for service-layer errors."""


class MasterSkuNotFoundError(ServiceError):
    """Raised when a referenced master_sku_id does not exist."""


class MappingNotFoundError(ServiceError):
    """Raised when a (channel, channel_sku) has no active mapping."""


class AmbiguousChannelSkuError(ServiceError):
    """Raised when a channel SKU identifies more than one product.

    In practice this is the empty string. Shopify sends no SKU for a variant
    that has none set, the adapter stored it verbatim, and the empty key then
    behaved like any other: `mapping_alerts` is UNIQUE on
    (channel, channel_sku, marketplace_id), so every such product collapsed
    into one alert carrying whichever 商品名 arrived first.

    Production on 2026-09-23 had 46 lines and ¥385,660 behind one empty
    Shopify key, spread over 9 different products. Resolving that alert would
    have attributed all of them to a single anklet — and, replayed with stock
    applied, decremented that anklet for sales of bracelets and rings.
    """


class InventoryInsufficientError(ServiceError):
    """Raised when an adjustment would push stock negative (manual_adjust only)."""
