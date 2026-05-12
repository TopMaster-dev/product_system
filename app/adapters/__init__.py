"""Channel adapters — Rakuten / Shopify / (future: Amazon / Wholesale).

All adapters implement the `ChannelAdapter` ABC, keeping channel specifics
isolated from the core inventory logic.
"""

from app.adapters.base import ChannelAdapter, NormalizedOrder, NormalizedOrderLine
from app.adapters.rakuten import RakutenAdapter
from app.adapters.rate_limit import TokenBucket
from app.adapters.shopify import ShopifyAdapter

__all__ = [
    "ChannelAdapter",
    "NormalizedOrder",
    "NormalizedOrderLine",
    "RakutenAdapter",
    "ShopifyAdapter",
    "TokenBucket",
]
