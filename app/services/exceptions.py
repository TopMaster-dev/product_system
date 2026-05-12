"""Domain exceptions raised by services."""

from __future__ import annotations


class ServiceError(Exception):
    """Base class for service-layer errors."""


class MasterSkuNotFoundError(ServiceError):
    """Raised when a referenced master_sku_id does not exist."""


class MappingNotFoundError(ServiceError):
    """Raised when a (channel, channel_sku) has no active mapping."""


class InventoryInsufficientError(ServiceError):
    """Raised when an adjustment would push stock negative (manual_adjust only)."""
