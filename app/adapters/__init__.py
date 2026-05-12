"""Channel adapters — Rakuten / Shopify / (future: Amazon / Wholesale).

All adapters implement the `ChannelAdapter` ABC, keeping channel specifics
isolated from the core inventory logic.
"""

from app.adapters.base import ChannelAdapter, NormalizedOrder, NormalizedOrderLine

__all__ = ["ChannelAdapter", "NormalizedOrder", "NormalizedOrderLine"]
