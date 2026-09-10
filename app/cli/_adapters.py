"""One place to build a channel adapter from settings.

Two CLIs had already copied this factory verbatim — they differed only in the
word used in the error message — and the Shopify stock audit needed a third.
docs/23 §1-12 is explicit that shared code lands once; an unowned helper copied
a third time is how the copies start drifting.

`purpose` only shapes the error text, but it is worth keeping: "credentials are
missing, cannot push" and "cannot audit stock" send an operator to different
places, and the whole reason the copies existed was that one message.
"""

from __future__ import annotations

from app.adapters import ShopifyAdapter
from app.config import get_settings


def build_shopify_adapter(*, purpose: str) -> ShopifyAdapter:
    settings = get_settings()
    if not settings.shopify_shop_domain or not settings.shopify_access_token:
        raise RuntimeError(f"Shopify credentials are missing from settings; cannot {purpose}.")
    return ShopifyAdapter(
        shop_domain=settings.shopify_shop_domain,
        access_token=settings.shopify_access_token,
        webhook_secret=settings.shopify_webhook_secret,
        api_version=settings.shopify_api_version,
        location_id=settings.shopify_location_id,
    )


__all__ = ["build_shopify_adapter"]
