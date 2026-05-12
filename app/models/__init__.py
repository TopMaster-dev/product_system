"""SQLAlchemy ORM models.

Concrete models land in Sprint 1:
- MasterSku, ChannelSkuMapping
- Order, OrderItem
- InventoryEvent, InventorySnapshot
- MappingAlert, WebhookLog
"""

from app.models.base import Base

__all__ = ["Base"]
